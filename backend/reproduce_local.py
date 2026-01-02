
import os
import sys

# Mock necessary env vars
if "DATABASE_URL" not in os.environ:
    os.environ["DATABASE_URL"] = "sqlite:///slots.db"

try:
    from scraper import fetch_all_pages
    from storage import upsert_slots, engine, Slot
    from sqlmodel import SQLModel
except ImportError:
    sys.path.append(os.getcwd())
    from scraper import fetch_all_pages
    from storage import upsert_slots, engine, Slot
    from sqlmodel import SQLModel

def main():
    print("Starting reproduction with OPTIMIZED scraper & STORAGE check...")
    
    # Init DB
    SQLModel.metadata.create_all(engine)
    
    try:
        # 1. Scrape
        results = fetch_all_pages(max_pages=3) # 3 pages enough for valid data
        print(f"Scrape finished. Found {len(results)} items.")

        if not results:
            print("No items found to test storage!")
            return

        # 2. First Upsert (All should be new)
        print("--- Round 1: Upsert (Expecting Opens) ---")
        opened, updated, _, _, _ = upsert_slots(results)
        print(f"Round 1 Result: opened={opened}, updated={updated}")
        
        # 3. Second Upsert (All should be existing/no-op unless changed)
        print("--- Round 2: Upsert (Expecting Updates/No-ops) ---")
        opened2, updated2, _, _, _ = upsert_slots(results)
        print(f"Round 2 Result: opened={opened2}, updated={updated2}")
        
        if opened2 == 0:
            print("SUCCESS: Batch logic correctly identified existing slots.")
        else:
            print(f"WARNING: Batch logic failed? Re-opened {opened2} slots that should exist.")

    except Exception as e:
        print(f"Test failed with error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
