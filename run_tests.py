import subprocess
import sys
import webbrowser
import os

def main():
    print("🚀 Running tests and generating coverage report...")

    # Run pytest with coverage flags
    cmd = [sys.executable, "-m", "pytest", "--cov=.", "--cov-report=term-missing", "--cov-report=html"]
    result = subprocess.run(cmd)

    if result.returncode == 0:
        print("\n✅ All tests passed!")
        html_path = os.path.abspath("htmlcov/index.html")
        if os.path.exists(html_path):
            print(f"📊 Opening coverage report: {html_path}")
            webbrowser.open(f"file://{html_path}")
    else:
        print("\n❌ Tests failed. Check the output above for details.")

    sys.exit(result.returncode)

if __name__ == "__main__":
    main()