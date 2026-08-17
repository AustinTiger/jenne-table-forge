import json
import re
from pathlib import Path

base_dir = Path(__file__).parent.parent.parent / "data" / "tables" / "individual"

modified_count = 0

gender_keywords = {"male", "female", "man", "woman"}

def should_split(text, filename, N, W):
    text_cleaned = text.strip()
    fn_lower = filename.lower()
    
    # Clean potential list brackets/quotes for checks
    cleaned_text = text_cleaned.replace("[", "").replace("]", "").replace("'", "").replace('"', "")
    parts = [p.strip() for p in cleaned_text.split(",") if p.strip()]
    N_clean = len(parts)
    
    if N_clean < 3:
        return False
        
    # Ignore tables with split/part/table descriptive text
    if any(w in cleaned_text.lower() for w in ["table", "part", "split"]):
        return False
        
    # If any part represents gender/profession context, do NOT split
    if any(p.lower() in gender_keywords for p in parts):
        return False
        
    # 1. Bracketed list representations like "['item1', 'item2']" are always lists and should be split
    if (text_cleaned.startswith("[") and text_cleaned.endswith("]")) or (text_cleaned.startswith("['") and text_cleaned.endswith("']")) or (text_cleaned.startswith('["') and text_cleaned.endswith('"]')):
        return True
        
    # 2. If it's a name table, split if N_clean >= 4
    if "names" in fn_lower and "occupations" not in fn_lower and "npc" not in fn_lower:
        if N_clean >= 4:
            return True
            
    # 3. If it's Sweet Breads or Fictional Spices or general list tables, split if N_clean >= 5
    if any(x in fn_lower for x in ["breads", "spices", "herbs", "plants", "ingredients"]):
        if N_clean >= 5 and N_clean > W:
            # Avoid splitting single entries with descriptors like "Pepper, black, white, and green"
            if not any(p.lower().startswith("and ") for p in parts):
                return True
                
    return False

for p in base_dir.glob("**/*.json"):
    with open(p, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except Exception:
            continue
            
    results = data.get("results", [])
    if not results:
        continue
        
    new_results = []
    shift = 0
    file_modified = False
    
    for idx, r in enumerate(results):
        start = r['range'][0] + shift
        end = r['range'][1] + shift
        r['range'] = [start, end]
        
        text = r.get("text", "")
        cleaned_text = text.replace("[", "").replace("]", "").replace("'", "").replace('"', "")
        parts = [p.strip() for p in cleaned_text.split(",") if p.strip()]
        
        N = len(parts)
        W = end - start + 1
        
        if should_split(text, p.name, N, W):
            print(f"Splitting result in {p.relative_to(base_dir)} (Index {idx+1}): {text[:60]}... into {N} entries.")
            for p_idx, p_text in enumerate(parts):
                new_r = {
                    "_id": r.get("_id", f"tfresult_{idx}") + f"_{p_idx}",
                    "type": r.get("type", 0),
                    "text": p_text,
                    "weight": 1,
                    "range": [start + p_idx, start + p_idx],
                    "drawn": False,
                    "flags": r.get("flags", {}),
                    "img": r.get("img", "icons/svg/d20-black.svg")
                }
                new_results.append(new_r)
            shift += (N - W)
            file_modified = True
        else:
            r["weight"] = W
            new_results.append(r)
            
    if file_modified:
        data["results"] = new_results
        # Update formula
        if new_results:
            max_val = new_results[-1]["range"][-1]
            data["formula"] = f"1d{max_val}"
            
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        modified_count += 1

print(f"\nCompleted! Cleaned and split {modified_count} files.")
