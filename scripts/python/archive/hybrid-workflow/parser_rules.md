# VTT-TableForge Parser Rules

This file defines the matching rules the Step 4 Python state machine (table slicer) uses to identify table headers, table rows, and skip lines.

---

## Pipeline Overview

```
Step 1: OCR        Docling PDF → data/raw_markdown/{stem}_raw.md
Step 2: Flatten    Python state machine: multi-column pipe tables → data/flat_markdown/{stem}_flat.md
Step 3: Clean      LLM (Gemini/Ollama): OCR typo fix → data/clean_markdown/{stem}_clean.md
Step 4: Split      Python state machine (rules below): _clean.md → data/extracted_tables/{stem}/*.md
Step 5: Compile    LLM or heuristic: table .md → data/tables/individual/{stem}/*.json
```

### CLI Usage
```
# Run all steps
python tableforge.py

# Run ONLY a specific step
python tableforge.py --step 2        # Flatten only
python tableforge.py --step 3        # LLM clean only

# Start at a specific step and run to the end
python tableforge.py --start-at 2    # Flatten → Clean → Split → Compile
python tableforge.py --start-at 4    # Split → Compile
```

---

## Step 2 — Column Flattener (State Machine)

Step 2 is a pure-Python, LLM-free state machine that reads `_raw.md` and converts
any multi-column pipe-table layout into a sequential single-column numbered list.

**Why this exists**: PDF source books often lay out wide random tables in 2 or 3 columns
to save space. Docling preserves this layout as `| col1 | col2 |` pipe rows. The LLM
in Step 3 cannot reliably flatten these when a table spans an LLM chunk boundary.
Step 2 processes the whole file in one pass, eliminating the boundary problem.

**Algorithm**:
1. Scan lines one at a time.
2. When a run of `| ... | ... |` rows is detected, buffer them.
3. On exit (blank line or non-pipe line), inspect column count:
   - 2 columns → emit left column entries, then right column entries.
   - 3 columns → emit left, then middle, then right.
   - 1 column  → pass cells through as plain lines.
   - Separator rows (`|---|---|`) are discarded.
4. Each non-empty cell is written as its own plain-text line.
5. Non-pipe lines are passed through unchanged.

**Output directory**: `data/flat_markdown/`

---

## Step 4 — Table Slicer (State Machine)

The rules below are used by Step 4 to slice `_clean.md` into individual table files.

### Header Patterns
These patterns detect the start of a table. The state machine extracts the table name from the match.
* `^\s*#+\s+(?P<title>.*)`
* `^\s*(?P<title>.*Table\s*#?\s*\d+.*)`
* `^\s*(?P<title>Roll\s+on\s+.*)`

### Entry Patterns
These patterns match rollable rows (e.g. "1. Item", "1-5. Item").
* `^\s*\|?\s*(?P<start>\d+)(?:\s*-\s*(?P<end>\d+))?[\.\s\|]+(?P<text>.*)`
* `^\s*\|\s*(?P<start>\d+)(?:\s*-\s*(?P<end>\d+))?\s*\|\s*(?P<text>.*)\s*\|`

### Skip Patterns
Lines matching these patterns are ignored during slicing.
* `^\s*<!--.*-->\s*$`
* `^\s*Credits\.*`
* `^\s*Visit\s+https:.*`
