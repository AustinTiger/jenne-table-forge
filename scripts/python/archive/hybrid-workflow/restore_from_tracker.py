import json
from pathlib import Path
import re

tracker_path = Path(__file__).parent / "tableforge_tracker.json"
output_dir = Path(__file__).parent.parent.parent

if not tracker_path.exists():
    print("Tracker file not found!")
    exit(1)

with open(tracker_path, "r", encoding="utf-8") as f:
    tracker = json.load(f)

for pdf_key, pdf_data in tracker.items():
    pdf_file = Path(pdf_data["path"])
    tables = pdf_data.get("tables_data", [])
    if not tables:
        continue
        
    pdf_folder = re.sub(r"[^\w-]", "_", pdf_file.name.lower().replace(".pdf", "").strip())
    pdf_folder = re.sub(r"_+", "_", pdf_folder).strip("_")
    
    sub_dir = output_dir / "data" / "tables" / "individual" / pdf_folder
    sub_dir.mkdir(parents=True, exist_ok=True)
    
    # Wipe the directory to start fresh
    for f_old in sub_dir.glob("*.json"):
        try:
            f_old.unlink()
        except Exception:
            pass
            
    print(f"Restoring {len(tables)} tables for {pdf_file.name}...")
    for idx, table in enumerate(tables):
        clean_table_name = re.sub(r"[^\w-]", "_", table["name"].lower().strip())
        clean_table_name = re.sub(r"_+", "_", clean_table_name).strip("_")
        page = table.get("page_number", 1)
        filename = f"{clean_table_name}_p{page}.json"
        
        extracted_desc = table.get("description", "").strip()
        if extracted_desc:
            description_text = f"{extracted_desc}\n\n[Extracted from {table['source_pdf']} (Page {table.get('page_number', 'N/A')})]"
        else:
            description_text = f"Extracted from {table['source_pdf']} (Page {table.get('page_number', 'N/A')})"
            
        foundry_results = []
        for r_idx, r in enumerate(table["results"]):
            foundry_results.append({
                "_id": f"tfresult{idx:04d}{r_idx:04d}",
                "type": 0,
                "text": r["text"],
                "weight": r.get("weight", 1),
                "range": r["range"],
                "drawn": False,
                "flags": {
                    "jenne-table-forge-importer": {
                        "metadata": r.get("metadata", {})
                    }
                },
                "img": "icons/svg/d20-black.svg"
            })
            
        formula = "1d100"
        if table.get("results"):
            try:
                formula = f"1d{table['results'][-1]['range'][-1]}"
            except Exception:
                pass
                
        foundry_table = {
            "_id": f"tftable{idx:09d}",
            "name": table["name"],
            "img": "icons/svg/d20-grey.svg",
            "description": description_text,
            "results": foundry_results,
            "formula": table.get("formula", formula),
            "replacement": True,
            "displayRoll": True,
            "folder": None,
            "flags": {
                "jenne-table-forge-importer": {
                    "source": table["source_pdf"],
                    "page": table.get("page_number", 0),
                    "is_master": False,
                    "merged_sources": []
                }
            }
        }
        
        with open(sub_dir / filename, "w", encoding="utf-8") as f_out:
            json.dump(foundry_table, f_out, indent=2, ensure_ascii=False)

print("Restore complete!")
