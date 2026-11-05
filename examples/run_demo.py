"""Sample crawling demonstration.

Starts the controlled local test website, crawls it with WebCrawler Pro (JavaScript
auto-rendering on), and writes CSV + XLSX into examples/sample_output/.

    python examples/run_demo.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.fixtures.site_server import TestSite  # noqa: E402

out = ROOT / "examples" / "sample_output"
shutil.rmtree(out, ignore_errors=True)
site = TestSite().start()
try:
    print(f"Test website: {site.base_url}")
    subprocess.run([sys.executable, "main.py", "--url", site.base_url, "--accept-authorization",
                    "--allow-private-network", "--delay", "0.2", "--depth", "5", "--javascript", "auto",
                    "--download-images", "--output-dir", str(out)], cwd=ROOT, check=False)
finally:
    site.stop()
print(f"\nResults in {out}")
