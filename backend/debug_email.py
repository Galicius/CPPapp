
import os
import sys

# Mock storage to avoid DB connection
class MockSupabase:
    def table(self, name): return self
    def select(self, *args): return self
    def eq(self, *args): return self
    def execute(self): return type('obj', (object,), {'data': []})

sys.modules['storage'] = type('module', (object,), {
    '_get_supabase_client': lambda: MockSupabase(), 
    'log_scrape_result': lambda *args, **kwargs: None
})

# Import notifications after mocking
try:
    from notifications import _render_email
except ImportError:
    sys.path.append(os.getcwd())
    from notifications import _render_email

def main():
    # Mock data
    sub = {
        "filter_obmocje": 1,
        "filter_town": "Ljubljana",
        "filter_categories": "B",
        "filter_exam_type": "voznja",
        "unsubscribe_token": "mock-token-123"
    }
    
    items = [
        {
            "date_str": "12. 1. 2026",
            "time_str": "08:00",
            "location": "LJUBLJANA - Roška cesta 25",
            "categories": "B",
            "exam_type": "voznja",
            "places_left": 1
        },
        {
            "date_str": "12. 1. 2026",
            "time_str": "10:30",
            "location": "LJUBLJANA - Roška cesta 25",
            "categories": "B", 
            "exam_type": "voznja",
            "places_left": 2
        },
         {
            "date_str": "15. 1. 2026",
            "time_str": "10:30",
            "location": "DOMŽALE - Ljubljanska cesta 12",
            "categories": "B, B1", 
            "exam_type": "teorija",
            "places_left": 15
        }
    ]

    subject, text, html = _render_email(sub, items)
    
    out_file = "debug_email.html"
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(html)
    
    print(f"Generated {out_file}. Open it in a browser to verify design.")

if __name__ == "__main__":
    main()
