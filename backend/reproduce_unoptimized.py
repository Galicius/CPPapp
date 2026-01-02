
import os
import sys
import time

# Mock necessary env vars
if "DATABASE_URL" not in os.environ:
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"

try:
    from scraper import fetch_all_pages
except ImportError:
    sys.path.append(os.getcwd())
    from scraper import fetch_all_pages

def main():
    print("Starting reproduction with UNOPTIMIZED scraper...")
    t0 = time.time()
    try:
        # Limit max pages for test, same as optimized test
        results = fetch_all_pages(max_pages=6)
        dur = time.time() - t0
        print(f"Scrape finished in {dur:.2f}s. Found {len(results)} items.")
    except Exception as e:
        print(f"Scrape failed with error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
