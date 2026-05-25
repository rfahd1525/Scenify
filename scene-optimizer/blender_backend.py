"""
Blender Backend
Orchestrates headless Blender subprocess calls for scene analysis,
asset extraction, and preview rendering.

All Blender operations run in the background — user never sees Blender.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import platform
from dataclasses import dataclass, field
from typing import Optional, Callable, Dict, List, Any


# Default Blender paths per platform
BLENDER_PATHS = {
    "Darwin": "/Applications/Blender.app/Contents/MacOS/Blender",
    "Windows": r"C:\Program Files\Blender Foundation\Blender 4.0\blender.exe",
    "Linux": "/usr/bin/blender",
}

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "blender_scripts")


@dataclass
class BlenderResult:
    """Result from a headless Blender operation."""
    success: bool
    data: Optional[Dict] = None
    error: Optional[str] = None
    output_path: Optional[str] = None
    stdout: str = ""
    stderr: str = ""


def find_blender() -> Optional[str]:
    """Find the Blender executable."""
    system = platform.system()

    # Check platform default
    default_path = BLENDER_PATHS.get(system)
    if default_path and os.path.isfile(default_path):
        return default_path

    # macOS app bundle
    if system == "Darwin":
        app_path = "/Applications/Blender.app/Contents/MacOS/Blender"
        if os.path.isfile(app_path):
            return app_path

    # Try PATH
    try:
        result = subprocess.run(
            ["which" if system != "Windows" else "where", "blender"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split('\n')[0]
    except Exception:
        pass

    return None


class BlenderBackend:
    """Manages headless Blender operations."""

    def __init__(self, blender_path: Optional[str] = None, output_dir: Optional[str] = None):
        self.blender_path = blender_path or find_blender()
        default_output = os.path.join(tempfile.gettempdir(), "scene_optimizer", "output")
        self.output_dir = output_dir or default_output
        # Staging dir is always in /tmp — Blender can always write here regardless of TCC
        self._staging_dir = os.path.join(tempfile.gettempdir(), "scene_optimizer", "staging")
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self._staging_dir, exist_ok=True)

        self._cancel_flag = False

    @property
    def is_available(self) -> bool:
        return self.blender_path is not None and os.path.isfile(self.blender_path)

    def cancel(self):
        """Cancel the current operation."""
        self._cancel_flag = True
        if self._current_process:
            try:
                self._current_process.terminate()
            except Exception:
                pass

    def analyze_scene(
        self,
        input_file: str,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> BlenderResult:
        """
        Analyze a scene file headlessly.
        Returns BlenderResult with scene manifest data.
        """
        output_json = os.path.join(self.output_dir, "scene_manifest.json")
        script = os.path.join(SCRIPTS_DIR, "analyze_scene.py")

        result = self._run_blender_script(
            script,
            [input_file, output_json],
            progress_callback=progress_callback,
            timeout=600,  # 10 minutes for large scenes
        )

        if result.success and os.path.exists(output_json):
            try:
                with open(output_json, 'r') as f:
                    result.data = json.load(f)
                result.output_path = output_json
            except json.JSONDecodeError as e:
                result.success = False
                result.error = f"Failed to parse manifest JSON: {e}"

        return result

    def extract_assets(
        self,
        input_file: str,
        manifest_path: Optional[str] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
        mode: str = "single",
        center_pivots: bool = True,
    ) -> BlenderResult:
        """
        Extract unique assets as GLB files.
        Requires a manifest from analyze_scene().

        Args:
            mode: "single" (one combined GLB, default) or "individual" (one GLB per asset).
            center_pivots: if True, each asset's mesh is re-centered on its bbox so
                Roblox MeshPart pivots land at the geometric center. Instance positions
                are adjusted accordingly in the extraction results.

        Blender writes to a /tmp staging dir (always TCC-accessible), then
        results are moved to self.output_dir by the main process.
        """
        if manifest_path is None:
            manifest_path = os.path.join(self.output_dir, "scene_manifest.json")

        if not os.path.exists(manifest_path):
            return BlenderResult(
                success=False,
                error="No scene manifest found. Run analysis first.",
            )

        # Blender writes here — always in /tmp to avoid macOS TCC blocks
        staging_extract = os.path.join(self._staging_dir, "extracted_assets")
        if os.path.exists(staging_extract):
            shutil.rmtree(staging_extract)
        os.makedirs(staging_extract)

        # Manifest must also be in /tmp for Blender to read it
        staging_manifest = os.path.join(self._staging_dir, "scene_manifest.json")
        shutil.copy2(manifest_path, staging_manifest)

        script = os.path.join(SCRIPTS_DIR, "extract_assets.py")

        result = self._run_blender_script(
            script,
            [
                input_file, staging_extract, staging_manifest,
                "--mode", mode,
                "--center-pivots", "true" if center_pivots else "false",
            ],
            progress_callback=progress_callback,
            timeout=1800,  # 30 minutes for large scenes
        )

        # Move results from staging to the user's chosen output dir
        final_extract_dir = os.path.join(self.output_dir, "extracted_assets")
        if os.path.exists(final_extract_dir):
            shutil.rmtree(final_extract_dir)
        if os.path.exists(staging_extract):
            shutil.move(staging_extract, final_extract_dir)
        else:
            final_extract_dir = staging_extract  # fallback

        # Load extraction results
        results_path = os.path.join(final_extract_dir, "_extraction_results.json")
        if os.path.exists(results_path):
            try:
                with open(results_path, 'r') as f:
                    result.data = json.load(f)
                result.data["outputDir"] = final_extract_dir
                combined = result.data.get("combinedFile")
                if combined:
                    result.data["combinedFile"] = os.path.join(
                        final_extract_dir, os.path.basename(combined)
                    )
                for asset in result.data.get("assets", []):
                    path = asset.get("path")
                    if path:
                        asset["path"] = os.path.join(
                            final_extract_dir, os.path.basename(path)
                        )
                with open(results_path, 'w') as f:
                    json.dump(result.data, f, indent=2)
                result.output_path = final_extract_dir
            except json.JSONDecodeError:
                pass

        return result

    def render_preview(
        self,
        input_file: str,
        width: int = 1280,
        height: int = 720,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> BlenderResult:
        """
        Render a scene preview image.
        Returns BlenderResult with path to the PNG.
        """
        output_png = os.path.join(self.output_dir, "scene_preview.png")
        script = os.path.join(SCRIPTS_DIR, "render_preview.py")

        result = self._run_blender_script(
            script,
            [input_file, output_png, str(width), str(height)],
            progress_callback=progress_callback,
            timeout=90,  # 90 seconds max for preview
        )

        if result.success and os.path.exists(output_png):
            result.output_path = output_png
        elif result.success:
            result.success = False
            result.error = "Preview render completed but output file not found."

        return result

    def _run_blender_script(
        self,
        script_path: str,
        args: List[str],
        progress_callback: Optional[Callable[[str], None]] = None,
        timeout: int = 600,
    ) -> BlenderResult:
        """Run a Blender Python script headlessly. Thread-safe."""
        if not self.is_available:
            return BlenderResult(
                success=False,
                error="Blender not found. Install Blender or set the path in settings.",
            )

        # macOS blocks Blender from reading files in certain directories
        # ("Operation not permitted"). Copy the script to /tmp as a workaround.
        tmp_script = os.path.join(tempfile.gettempdir(), f"blender_{os.path.basename(script_path)}")
        shutil.copy2(script_path, tmp_script)

        cmd = [
            self.blender_path,
            "--background",
            "--python", tmp_script,
            "--",
        ] + args

        stdout_lines = []
        stderr_lines = []
        proc = None

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            # Stream stdout for progress
            success = False
            for line in proc.stdout:
                line = line.strip()
                stdout_lines.append(line)

                if self._cancel_flag:
                    proc.terminate()
                    return BlenderResult(
                        success=False,
                        error="Operation cancelled by user.",
                        stdout="\n".join(stdout_lines),
                    )

                if line.startswith("PROGRESS:") and progress_callback:
                    progress_callback(line[9:])
                elif line == "RESULT:SUCCESS":
                    success = True

            # Capture remaining stderr
            stderr_output = proc.stderr.read()
            if stderr_output:
                stderr_lines.append(stderr_output)

            proc.wait(timeout=30)

            return BlenderResult(
                success=success,
                error=None if success else "Blender script did not report success.",
                stdout="\n".join(stdout_lines),
                stderr="\n".join(stderr_lines),
            )

        except subprocess.TimeoutExpired:
            if proc:
                proc.kill()
            return BlenderResult(
                success=False,
                error=f"Operation timed out after {timeout} seconds.",
                stdout="\n".join(stdout_lines),
                stderr="\n".join(stderr_lines),
            )
        except FileNotFoundError:
            return BlenderResult(
                success=False,
                error=f"Blender executable not found at: {self.blender_path}",
            )
        except Exception as e:
            return BlenderResult(
                success=False,
                error=f"Unexpected error: {str(e)}",
                stdout="\n".join(stdout_lines),
                stderr="\n".join(stderr_lines),
            )
        finally:
            if proc:
                try:
                    proc.stdout.close()
                    proc.stderr.close()
                except Exception:
                    pass

    def run_async(
        self,
        method: str,
        kwargs: Dict[str, Any],
        done_callback: Optional[Callable[[BlenderResult], None]] = None,
    ) -> threading.Thread:
        """
        Run a backend method asynchronously in a background thread.
        Calls done_callback on completion (from the background thread).
        """
        def _worker():
            func = getattr(self, method)
            result = func(**kwargs)
            if done_callback:
                done_callback(result)

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        return thread
