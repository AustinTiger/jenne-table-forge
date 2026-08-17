"""
VTT-TableForge: Professional RPG Table Extraction Pipeline
----------------------------------------------------------
Step 1: OCR        (Docling) -> data/raw_markdown/
Step 2: Flatten    (Python)  -> data/flat_markdown/
Step 3: Clean      (Local)   -> data/clean_markdown/
Step 4: Split      (Python)  -> data/extracted_tables/
Step 5: Compile    (Ollama)  -> data/tables/individual/
Step 6: Manifest   (Python)  -> data/metadata.json
"""

import os
import re
import time
import json
import argparse
import datetime
import logging
import threading
import shutil
import requests
import urllib.parse
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from tqdm import tqdm
from pypdf import PdfReader, PdfWriter
from dotenv import load_dotenv

# --- HARDWARE TUNING ---
os.environ["DOCLING_DEVICE"] = "cpu"
os.environ["DOCLING_NUM_THREADS"] = "12" 

# --- GLOBAL PATHS ---
DEFAULT_OUTPUT_PATH = Path(__file__).parent.parent.parent.resolve()

# --- LOGGING SETUP ---
logging.basicConfig(level=logging.WARNING, force=True)
for noisy in ["RapidOCR", "docling", "pdfminer", "onnxruntime", "huggingface_hub", "urllib3"]:
    logging.getLogger(noisy).setLevel(logging.WARNING)

# --- THREAD-SAFE GLOBAL STATES ---
GLOBAL_LOGS = []
GLOBAL_PROGRESS = 0
IS_RUNNING = False
CANCEL_EVENT = threading.Event()

def log_event(msg, log_type="info"):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    formatted = f"[{timestamp}] [{log_type.upper()}] {msg}"
    print(formatted)
    global GLOBAL_LOGS
    # Cap logs in memory to avoid leaking RAM during large bulk runs
    if len(GLOBAL_LOGS) > 1000:
        GLOBAL_LOGS.pop(0)
    GLOBAL_LOGS.append({"type": log_type, "message": msg})

class TableForgeExtractor:
    def __init__(self, pdf_dirs, output_dir, skip_processed=False):
        load_dotenv(Path(__file__).parent / "tableforge.env")
        self.pdf_dirs = [Path(d) for d in pdf_dirs]
        self.output_dir = Path(output_dir)
        self.skip_processed = skip_processed
        self.tracker = {}
        self.load_tracker()
        
        self.ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434").strip()
        self.ollama_model = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b").strip()
        
        # Load Matching Patterns from parser_rules.md
        self.parser_rules = self.load_parser_rules(Path(__file__).parent / "parser_rules.md")
        
        try:
            from docling.document_converter import DocumentConverter
            self.doc_converter = DocumentConverter()
            log_event("Docling layout engine initialized (CPU Mode).", "success")
        except Exception as e:
            self.doc_converter = None
            log_event(f"Docling init failed: {e}.", "error")

    def load_tracker(self):
        tp = Path(__file__).parent / "tableforge_tracker.json"
        if tp.exists():
            with open(tp, "r", encoding="utf-8") as f: self.tracker = json.load(f)

    def save_tracker(self):
        tp = Path(__file__).parent / "tableforge_tracker.json"
        with open(tp, "w", encoding="utf-8") as f: json.dump(self.tracker, f, indent=2)

    def load_parser_rules(self, rules_path):
        """Generic Rule Loader: Compiles regex strings from your markdown file."""
        rules = {"headers": [], "entries": [], "skips": []}
        if not rules_path.exists():
            log_event("Rules file missing. Using hardcoded defaults.", "warn")
            rules["headers"] = [re.compile(r"^\s*#+\s+(?P<title>.*)")]
            rules["entries"] = [re.compile(r"^\s*[\-\–\—\•\*\|]?\s*(?P<start>\d+)")]
            return rules

        current_section = None
        with open(rules_path, "r", encoding="utf-8") as f:
            for line in f:
                c = line.strip()
                if "Header Patterns" in c: current_section = "headers"
                elif "Entry Patterns" in c: current_section = "entries"
                elif "Skip Patterns" in c: current_section = "skips"
                elif (c.startswith("* `") or c.startswith("- `")) and c.endswith("`"):
                    pattern = c.split("`")[1]
                    try:
                        rules[current_section].append(re.compile(pattern, re.IGNORECASE))
                    except Exception as e:
                        log_event(f"Regex Error in {current_section}: {pattern} ({e})", "error")
        return rules

    # --- PIPELINE STAGES ---

    def run_stage_1_ocr(self, stem, source_path):
        out = self.output_dir / "data" / "raw_markdown" / f"{stem}_raw.md"
        if out.exists(): return True
        out.parent.mkdir(parents=True, exist_ok=True)
        log_event(f"Stage 1: OCR Analysis ({stem})")
        try:
            res = self.doc_converter.convert(source_path)
            with open(out, "w", encoding="utf-8") as f: f.write(res.document.export_to_markdown())
            return True
        except Exception as e:
            log_event(f"OCR Failed for {stem}: {e}", "error"); return False

    def process_table_columns(self, rows):
        """Analyze column classifications and merge/emit them intelligently."""
        if not rows:
            return []
        
        num_rows = len(rows)
        num_cols = len(rows[0])
        
        if num_rows == 1:
            return [" - ".join(rows[0]) + "\n"]
            
        range_pattern = re.compile(r'^\s*(?:d\d+|[-–—•*|]?\s*\d+(?:\s*[-–—]\s*\d+)?)\s*$', re.IGNORECASE)
        
        def is_range_str(s):
            s_clean = s.strip().strip('.').strip(':')
            return bool(range_pattern.match(s_clean))
            
        # Step 1: Identify horizontal slices
        # A row starts a new slice if it contains a range header (e.g. "1-20: Male", "21-40: Genres") 
        # or a specific heading string.
        slice_starts = [0]
        range_header_pat = re.compile(r'^\s*[\-\–\—\•\*\|]?\s*(?:\d+[-–—]\d+)\b', re.IGNORECASE)
        voice_header_pat = re.compile(r'Character voices I can do:', re.IGNORECASE)
        
        for r in range(1, num_rows):
            first_cell = rows[r][0].strip()
            row_cells = set(rows[r][c].strip() for c in range(num_cols) if rows[r][c].strip())
            is_heading_row = False
            if len(row_cells) == 1:
                val = list(row_cells)[0]
                if range_header_pat.search(val) or voice_header_pat.search(val):
                    is_heading_row = True
            elif range_header_pat.search(first_cell) or voice_header_pat.search(first_cell):
                is_heading_row = True
                
            if is_heading_row:
                slice_starts.append(r)
                
        output_lines = []
        
        # Process each horizontal slice independently
        for idx, start_row in enumerate(slice_starts):
            end_row = slice_starts[idx+1] if idx + 1 < len(slice_starts) else num_rows
            slice_rows = rows[start_row:end_row]
            slice_num_rows = len(slice_rows)
            
            # Analyze numbered columns within this slice (excluding header row 0)
            numbered_cols = []
            for c in range(num_cols):
                num_keys = 0
                non_empty = 0
                for r in range(1, slice_num_rows):
                    cell = slice_rows[r][c].strip()
                    if cell:
                        non_empty += 1
                        if re.match(r'^\s*[\-\–\—\•\*\|]?\s*\d+', cell):
                            num_keys += 1
                is_numbered = (num_keys / non_empty >= 0.25) if non_empty > 0 else False
                numbered_cols.append(is_numbered)
                
            # Case A: Standard single table with range/numbers in Col 0 only
            if numbered_cols[0] and not any(numbered_cols[1:]):
                header_text = " - ".join([slice_rows[0][c] for c in range(num_cols) if slice_rows[0][c]])
                output_lines.append(header_text + "\n")
                for r in range(1, slice_num_rows):
                    range_val = slice_rows[r][0].strip()
                    remaining = [slice_rows[r][c].strip() for c in range(1, num_cols) if slice_rows[r][c].strip()]
                    if range_val or remaining:
                        if range_val:
                            line_text = f"- {range_val} " + " ".join(remaining)
                        else:
                            line_text = " ".join(remaining)
                        output_lines.append(line_text + "\n")
                continue
                
            # If there are no side-by-side numbered columns, fall back to row-by-row joining
            num_numbered_cols = sum(1 for x in numbered_cols if x)
            if num_numbered_cols < 2:
                for r in range(slice_num_rows):
                    line_text = " ".join([slice_rows[r][c] for c in range(num_cols) if slice_rows[r][c]])
                    if line_text:
                        output_lines.append(line_text + "\n")
                continue
                
            # Case B: Multi-column side-by-side tables.
            # Determine column sub-table starts
            sub_starts = [0]
            for c in range(1, num_cols):
                if numbered_cols[c]:
                    sub_starts.append(c)
                    
            # Process each column block of this horizontal slice
            for i, start_col in enumerate(sub_starts):
                end_col = sub_starts[i+1] if i + 1 < len(sub_starts) else num_cols
                sub_cols = list(range(start_col, end_col))
                
                header_cells = [slice_rows[0][c].strip() for c in sub_cols]
                header_text = " - ".join([cell for cell in header_cells if cell])
                
                entry_split_pat = re.compile(r'^(?P<title>.*?)\s+(?P<entry>\b1\b\..*)$', re.IGNORECASE)
                right_entry_split_pat = re.compile(r'^(?P<title>.*?)\s+(?P<entry>\b11\b\..*)$', re.IGNORECASE)
                
                title = header_text
                swallowed_entry = None
                
                first_cell = slice_rows[0][start_col].strip()
                m = entry_split_pat.match(first_cell) or right_entry_split_pat.match(first_cell)
                if m:
                    title = m.group("title").strip()
                    swallowed_entry = m.group("entry").strip()
                    extra_header_cells = [slice_rows[0][c].strip() for c in sub_cols[1:] if slice_rows[0][c].strip()]
                    if extra_header_cells:
                        swallowed_entry = swallowed_entry + " " + " ".join(extra_header_cells)
                else:
                    if first_cell.startswith("1.") or first_cell.startswith("11."):
                        title = ""
                        swallowed_entry = first_cell
                        extra_header_cells = [slice_rows[0][c].strip() for c in sub_cols[1:] if slice_rows[0][c].strip()]
                        if extra_header_cells:
                            swallowed_entry = swallowed_entry + " " + " ".join(extra_header_cells)
                
                # Emit header if title is non-empty
                # Skip if it's a continuation of a duplicated header (i.e. start_col > 0 and cell is same as col 0)
                is_dup_header = False
                if start_col > 0 and slice_rows[0][start_col].strip() == slice_rows[0][0].strip():
                    is_dup_header = True
                    
                if title and not is_dup_header:
                    # Clean up duplicate names like "21-40: Genres - 21-40: Genres"
                    clean_title = title
                    parts = [p.strip() for p in title.split(" - ")]
                    if len(set(parts)) == 1:
                        clean_title = parts[0]
                    output_lines.append(f"## {clean_title}\n")
                    
                if swallowed_entry:
                    output_lines.append(f"- {swallowed_entry}\n")
                    
                for r in range(1, slice_num_rows):
                    cells = [slice_rows[r][c].strip() for c in sub_cols]
                    if not any(cells):
                        continue
                        
                    if len(sub_cols) > 1:
                        range_val = cells[0]
                        remaining = [cell for cell in cells[1:] if cell]
                        if range_val or remaining:
                            if range_val:
                                line_text = f"- {range_val} " + " ".join(remaining)
                            else:
                                line_text = " ".join(remaining)
                            output_lines.append(line_text + "\n")
                    else:
                        line_text = cells[0]
                        if line_text:
                            if not line_text.startswith("-") and not line_text.startswith("*"):
                                if re.match(r'^\s*\d+', line_text):
                                    line_text = f"- {line_text}"
                            output_lines.append(line_text + "\n")
        return output_lines

    def run_stage_2_flatten(self, stem, force=False):
        raw = self.output_dir / "data" / "raw_markdown" / f"{stem}_raw.md"
        out = self.output_dir / "data" / "flat_markdown" / f"{stem}_flat.md"
        if out.exists() and not force: return True
        if not raw.exists(): return False
        out.parent.mkdir(parents=True, exist_ok=True)
        log_event(f"Stage 2: Flattening {stem}")
        pipe_re, sep_re = re.compile(r'^\|(.+\|)+\s*$'), re.compile(r'^\|[\s\-:|]+$')
        with open(raw, "r", encoding="utf-8") as f:
            lines = [line.replace('\xa0', ' ').replace('&nbsp;', ' ') for line in f]
        final, buf = [], []
        def flush(b):
            if not b: return []
            rows = [r.strip().strip('|').split('|') for r in b if not sep_re.match(r.strip())]
            if not rows: return []
            rows = [[cell.strip() for cell in row] for row in rows]
            return self.process_table_columns(rows)
        for line in lines:
            if pipe_re.match(line.strip()):
                buf.append(line)
            else:
                if buf:
                    final.extend(flush(buf))
                    buf = []
                final.append(line)
        final.extend(flush(buf))
        with open(out, "w", encoding="utf-8") as f: f.writelines(final)
        return True

    def run_stage_3_clean(self, stem, force=False):
        flat = self.output_dir / "data" / "flat_markdown" / f"{stem}_flat.md"
        out = self.output_dir / "data" / "clean_markdown" / f"{stem}_clean.md"
        # Honor user lock — never overwrite a manually-edited clean file, even with --force
        stem_cfg = self.mappings.get("stems", {}).get(stem, {})
        if stem_cfg.get("clean_locked") and out.exists():
            log_event(f"  Stage 3: {stem}_clean.md is locked (user-edited) — skipping", "info")
            return True
        if out.exists() and not force: return True
        if not flat.exists(): return False
        out.parent.mkdir(parents=True, exist_ok=True)
        log_event(f"Stage 3: Cleaning {stem}")
        noise = [
            re.compile(r"^\d+$"),
            re.compile(r"^Page \d+.*", re.I),
            re.compile(r".*www\..*\.com.*", re.I),
            # OCR folio/running-header noise: "Word Word WORD" or "WORD WORD" repeated artifacts
            re.compile(r"^[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+[A-Z]{2,}\s*$"),
            re.compile(r"^[A-Z]{2,}(?:\s+[A-Z]{2,})+\s*$"),
        ]
        
        # Header pattern with number: starts with hashes, optional space, then digits, optional range, separator, text
        header_num_re = re.compile(r'^\s*(?!.*\.\.)(#+)\s+(\d+)(?:\s*[-–—]\s*(\d+))?[:.\s\|]+(.*)', re.IGNORECASE)
        # Generic header pattern
        header_re = re.compile(r'^\s*#+\s+(.*)')
        # Entry pattern: optional bullet, then digits, optional range, separator, text
        entry_re = re.compile(r'^\s*[\-\–\—\•\*\|]?\s*(\d+)(?:\s*[-–—]\s*(\d+))?[:.\s\|]+(.*)', re.IGNORECASE)
        
        # Regexes for inline entry splitting, reddit usernames, empty number entries
        inline_split_re = re.compile(r'(?<![\-\–\—\•\*\|])\s+(?=\d+(?:\s*[-–—]\s*\d+)?\.\s+[A-Za-z])')
        reddit_user_re = re.compile(r'\s*/\s*u\s*/\s*[A-Za-z0-9_-]+|\s*\bu\s*/\s*[A-Za-z0-9_-]+', re.IGNORECASE)
        empty_entry_re = re.compile(r'^\s*[\-\–\—\•\*\|]?\s*(\d+)(?:\s*[-–—]\s*(\d+))?[:.\s\|]*$', re.IGNORECASE)
        
        ligature_fixes = {
            r'\boffi\s+ce\b': 'office',
            r'\bhalfl\s+ing\b': 'halfling',
            r'\bfi\s+re\b': 'fire',
            r'\boff\s+er\b': 'offer',
            r'\boff\s+ering\b': 'offering',
            r'\bfl\s+oating\b': 'floating',
            r'\bfl\s+oor\b': 'floor',
            r'\bfl\s+oatsam\b': 'flotsam',
            r'\bcoffi\s+n\b': 'coffin',
            r'\btaaff\s+eite\b': 'taaffeite',
            r'\bdi\s+ff\s+erent\b|\bdiff\s+erent\b': 'different',
            r'\bseaf\s+aring\b': 'seafaring',
            r'\bwhirl\s+pool\b': 'whirlpool',
            r'\bwind\s+storm\b': 'windstorm',
            r'\bthunder\s+storm\b': 'thunderstorm',
            r'\bsand\s+storm\b': 'sandstorm',
            r'\beldirtch\b': 'eldritch',
        }
        def clean_ligature_spaces(text_val):
            pattern = re.compile(r'\b([a-zA-Z]*(?:fi|fl|ff|ffi|ffl))\s+([a-z]+)\b')
            def replacer(match):
                g1, g2 = match.groups()
                if g2 in ("in", "it", "on", "at", "an", "is", "of", "us", "up", "or", "by", "to", "if", "out", "the", "a", "and"):
                    return match.group(0)
                return g1 + g2
            return pattern.sub(replacer, text_val)

        def deduplicate_repeated_phrase(text_val):
            text_val = text_val.strip()
            if len(text_val) < 15:
                return text_val
            n = len(text_val)
            if n % 2 == 0:
                mid = n // 2
                p1 = text_val[:mid].strip()
                p2 = text_val[mid:].strip()
                if p1 == p2:
                    return p1
            # Try space-split check
            spaces = [idx_space for idx_space, c in enumerate(text_val) if c == ' ']
            for sp in spaces:
                p1 = text_val[:sp].strip()
                p2 = text_val[sp:].strip()
                p1_norm = re.sub(r'\W+', '', p1).lower()
                p2_norm = re.sub(r'\W+', '', p2).lower()
                if p1_norm == p2_norm and len(p1_norm) > 10:
                    return p1
            return text_val

        with open(flat, "r", encoding="utf-8") as f:
            lines = [line.replace('\xa0', ' ').replace('&nbsp;', ' ') for line in f]
            
        pre_cleaned = []
        double_entry_re = re.compile(r'^\s*[\-\–\—\•\*\|]?\s*(\d+)\.\s+(\d+)\.\s+(.*)', re.IGNORECASE)
        for line in lines:
            # Strip HTML comments
            line = re.sub(r'<!--.*?-->', '', line)
            s = line.strip()
            if not s or any(r.match(s) for r in noise): continue
            
            # Submitter cleanup
            s = reddit_user_re.sub("", s)
            
            # Unicode Ligature Pre-pass: replace U+FB00–U+FB06 codepoints with ASCII equivalents
            # Must run BEFORE ligature_fixes so the regex patterns can match cleanly
            s = s.replace('\ufb00', 'ff').replace('\ufb01', 'fi').replace('\ufb02', 'fl')
            s = s.replace('\ufb03', 'ffi').replace('\ufb04', 'ffl').replace('\ufb05', 'st').replace('\ufb06', 'st')

            # OCR Ligature cleanup
            for pattern, replacement in ligature_fixes.items():
                s = re.sub(pattern, replacement, s, flags=re.IGNORECASE)
                
            # Clean spaces in ligature splits (e.g. "fi   nds" -> "finds")
            s = clean_ligature_spaces(s)
            
            # Clean spaces before punctuation (e.g. " ." -> ".")
            s = re.sub(r'\s+([.,!?])', r'\1', s)
            
            # Deduplicate repeated column text on the same line
            s = deduplicate_repeated_phrase(s)
            
            # Expand double consecutive entries like "48. 49. Psychic vision oil Baboon skin"
            m = double_entry_re.match(s)
            if m:
                num1, num2, text = m.groups()
                prefix = ""
                if s.startswith("-") or s.startswith("*"):
                    prefix = s[0] + " "
                words = text.split()
                split_idx = -1
                for j in range(len(words) - 1, 1, -1):
                    w = words[j]
                    w_clean = re.sub(r'^\W+', '', w)
                    if w_clean and w_clean[0].isupper() and not w_clean.isupper():
                        split_idx = j
                        break
                if split_idx != -1:
                    part1 = " ".join(words[:split_idx])
                    part2 = " ".join(words[split_idx:])
                    candidates = [f"{prefix}{num1}. {part1}", f"{prefix}{num2}. {part2}"]
                else:
                    candidates = [f"{prefix}{num1}. {text}", f"{prefix}{num2}. {text}"]
            else:
                candidates = [s]
                
            for cand in candidates:
                # Inline splitting (multiple entries on same line)
                sublines = inline_split_re.split(cand)
                for subline in sublines:
                    sub_s = subline.strip()
                    if sub_s:
                        pre_cleaned.append(sub_s)
                    
        cleaned = []
        last_entry_num = None
        last_line_was_entry = False
        sub_list_in_progress = False
        
        i = 0
        while i < len(pre_cleaned):
            s = pre_cleaned[i].strip()
            
            # Empty number peeking and merging (e.g. "45." followed by "Lute")
            empty_match = empty_entry_re.match(s)
            if empty_match and i + 1 < len(pre_cleaned):
                next_s = pre_cleaned[i + 1].strip()
                if not (entry_re.match(next_s) or header_re.match(next_s) or header_num_re.match(next_s)):
                    s = f"{s} {next_s}"
                    i += 1
                    
            num_header_match = header_num_re.match(s)
            if num_header_match:
                hashes = num_header_match.group(1)
                start_num = int(num_header_match.group(2))
                end_num_val = num_header_match.group(3)
                end_num = int(end_num_val) if end_num_val else None
                text = num_header_match.group(4).strip()
                
                # Strip leading and trailing bullets/separators from parsed text
                text = re.sub(r'^[\-\–\—\•\*\|:\.\s]+', '', text).strip()
                text = re.sub(r'\s+[\-\–\—\•\*\|:\s]+$', '', text).strip()
                
                is_false_header = False
                if end_num is None:
                    if last_entry_num is not None:
                        if start_num == last_entry_num + 1:
                            is_false_header = True
                    else:
                        if start_num == 1:
                            is_false_header = True
                        
                if is_false_header:
                    range_str = f"{start_num}-{end_num}" if end_num else str(start_num)
                    corrected_line = f"- {range_str} {text}"
                    cleaned.append(corrected_line + "\n")
                    last_entry_num = end_num if end_num else start_num
                    last_line_was_entry = True
                    i += 1
                    continue
                else:
                    last_entry_num = None
                    last_line_was_entry = False
                    cleaned.append(s + "\n")
                    i += 1
                    continue
                    
            if header_re.match(s):
                last_entry_num = None
                last_line_was_entry = False
                sub_list_in_progress = False
                cleaned.append(s + "\n")
                i += 1
                continue
                
            is_sub_number = False
            if last_line_was_entry and not (s.startswith("-") or s.startswith("*") or s.startswith("•")):
                num_match = re.match(r'^(\d+)[\.\)]\s+', s)
                if num_match:
                    num_val = int(num_match.group(1))
                    if last_entry_num is not None and num_val <= last_entry_num:
                        if num_val == 1 or sub_list_in_progress:
                            is_sub_number = True
                            if num_val == 1:
                                sub_list_in_progress = True
            
            entry_match = None if is_sub_number else entry_re.match(s)
            if entry_match:
                start_num = int(entry_match.group(1))
                end_num_val = entry_match.group(2)
                end_num = int(end_num_val) if end_num_val else None
                if 1 <= start_num <= 100 and (end_num is None or end_num <= 100):
                    last_entry_num = end_num if end_num else start_num
                    sub_list_in_progress = False
                    text = entry_match.group(3).strip()
                    
                    # Strip leading and trailing bullets/separators from parsed text
                    text = re.sub(r'^[\-\–\—\•\*\|:\.\s]+', '', text).strip()
                    text = re.sub(r'\s+[\-\–\—\•\*\|:\s]+$', '', text).strip()
                    
                    range_str = f"{start_num}-{end_num}" if end_num else str(start_num)
                    # Rebuild to normalize spacing with a forced dash prefix
                    corrected_line = f"- {range_str}. {text}"
                        
                    cleaned.append(corrected_line + "\n")
                    last_line_was_entry = True
                    i += 1
                    continue
                
            # Continuation / description line merging
            # Guard against page-bleed: short title-case-only lines (e.g. "Sea Quests", "Forest Quests")
            # that bleed from the next page's section heading should NOT be appended to the last entry.
            is_section_bleed = (
                last_line_was_entry and
                bool(re.match(r'^[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}$', s)) and
                len(s.split()) <= 4
            )
            is_digit_start = bool(re.match(r'^\s*\d+', s))
            if last_line_was_entry and len(cleaned) > 0 and not is_section_bleed and not is_digit_start:
                prev_line = cleaned[-1].rstrip()
                # Check if this continuation line starts with a sub-number (e.g. "1. ", "2) ", "(a) ")
                if re.match(r'^(?:\d+[\.\)]|\([a-zA-Z0-9]+\))\s+', s):
                    cleaned[-1] = f"{prev_line}<br>{s}\n"
                else:
                    cleaned[-1] = f"{prev_line} {s}\n"
            else:
                if not is_section_bleed:
                    cleaned.append(s + "\n")
                last_line_was_entry = False
                
            i += 1
            
        # Specific override for Talking Inanimate Objects in book-of-random-tables4
        if stem == "book-of-random-tables4":
            new_lines = []
            in_talking = False
            for line in cleaned:
                if line.strip() == "## Talking Inanimate Objects":
                    new_lines.append(line)
                    in_talking = True
                    corrected_entries = [
                        "1-2. Belt buckle that claims to be a wizard\n",
                        "3-4. Iron bracelet that chats about the weather\n",
                        "5-6. Iron chest that tells the story of the fall of an empire\n",
                        "7-8. Burlap sack that recites love poetry\n",
                        "9-10. Wagon that tells sad stories\n",
                        "11-12. Wood file that claims to be a carpenter\n",
                        "13-14. Dagger that claims he's a prince\n",
                        "15-16. Longsword that tries to pick a fight with everyone\n",
                        "17-18. Arm ring that claims to be a merchant\n",
                        "19-20. Book that reads itself out loud\n",
                        "21-22. Block of wood that wants to be useful\n",
                        "23-24. Book that begs the finder not to read it\n",
                        "25-26. Bronze coin that talks like a goblin\n",
                        "27-28. Metal hook the clucks like a chicken\n",
                        "29-30. Iron bracelet that claims to have secret knowledge\n",
                        "31-32. Shirt that complains about being out of style\n",
                        "33-34. Sword that shrieks when drawn\n",
                        "35-36. Wooden club that barks every time it is swung\n",
                        "37-38. Scarf that likes idle chatter\n",
                        "39-40. Cloak that claims she's a princess\n",
                        "41-42. Hammer that claims to be a blacksmith\n",
                        "43-44. Bronze ring that asks many questions\n",
                        "45-46. Walking stick that claims to be a sorcerer\n",
                        "47-48. Pair of boots that complain all the time\n",
                        "49-50. Bar of soap that swears like a sailor\n",
                        "51-52. Chain that claims to be a dwarf\n",
                        "53-54. Leather belt that claims to be a king\n",
                        "55-56. Small polished rock that sings lullabies\n",
                        "57-58. Backpack that is depressed\n",
                        "59-60. Lantern that talks about the good old days\n",
                        "61-62. Broom that wishes to be useful\n",
                        "63-64. Butter knife that hurls insults\n",
                        "65-66. Large rock that mourns a lost loved one\n",
                        "67-68. Rope that claims to be an elf\n",
                        "69-70. Pipe that claims to be a halfling\n",
                        "71-72. Vial that laments being empty\n",
                        "73-74. Spoon that claims to be an old woman\n",
                        "75-76. Jade statuette that claims to be a great warrior\n",
                        "77-78. Quill that critiques the writing it is used to make\n",
                        "79-80. Gold necklace that compliments the wearer\n",
                        "81-82. Pair of gloves that claims to be a queen\n",
                        "83-84. Silver ring that sings whenever the moon is out\n",
                        "85-86. Brass button that talks like an orc\n",
                        "87-88. Pouch that claims to be a half elf\n",
                        "89-90. Cloak clasp that says it just wants to have friends\n",
                        "91-92. Fork that tells stories of heroic deeds\n",
                        "93-94. Silver bracelet that likes to discuss politics\n",
                        "95-96. Gold ring that likes to discuss history\n",
                        "97-98. Short sword that sings whenever it is drawn\n",
                        "99-100. Ring that chats about local celebrities\n"
                    ]
                    new_lines.extend(corrected_entries)
                    continue
                if in_talking:
                    if line.strip().startswith("## "):
                        in_talking = False
                    else:
                        continue
                new_lines.append(line)
            cleaned = new_lines

        with open(out, "w", encoding="utf-8") as f: f.writelines(cleaned)
        return True

    def run_stage_4_split(self, stem):
        """Step 4: Rules-based slicing with strict validation reset and sequence check."""
        clean = self.output_dir / "data" / "clean_markdown" / f"{stem}_clean.md"
        out_dir = self.output_dir / "data" / "extracted_tables" / stem
        if not clean.exists(): return False
        out_dir.mkdir(parents=True, exist_ok=True)
        # Purge old snippets
        for old in out_dir.glob("*.md"): old.unlink()

        log_event(f"Stage 4: Slicing {stem}")
        with open(clean, "r", encoding="utf-8") as f:
            lines = [line.replace('\xa0', ' ').replace('&nbsp;', ' ') for line in f]
        
        curr_table_buf, curr_name, has_entries = [], "unnamed", False
        last_entry_num = None
        seen_entries = set()
        pending_header = None # dict: {"name": str, "lines": list}

        def sort_table_buffer(buf):
            # Regex for identifying numbers at the start of entries for sorting
            entry_sort_re = re.compile(r'^\s*[\-\–\—\•\*\|]?\s*(\d+)', re.IGNORECASE)
            headers = []
            entries = []
            current_entry = None
            
            for line in buf:
                l_strip = line.strip()
                if not l_strip:
                    if current_entry: current_entry["lines"].append(line)
                    else: headers.append(line)
                    continue
                    
                entry_match = entry_sort_re.match(l_strip)
                if entry_match:
                    if current_entry: entries.append(current_entry)
                    start_num = int(entry_match.group(1))
                    current_entry = {"num": start_num, "lines": [line]}
                else:
                    if not current_entry and len(entries) == 0:
                        headers.append(line)
                    else:
                        if current_entry: current_entry["lines"].append(line)
                        else: headers.append(line)
                            
            if current_entry: entries.append(current_entry)
            entries.sort(key=lambda x: x["num"])
            
            new_buf = list(headers)
            for entry in entries: new_buf.extend(entry["lines"])
            return new_buf

        def renumber_table_buffer(buf):
            # Renumber sequentially and strip leading bullets universally
            entry_prefix_re = re.compile(r'^(\s*[\-\–\—\•\*\|]?\s*)\d+(?:\s*[-–—]\s*\d+)?([:.\s\|]+)(.*)', re.IGNORECASE)
            new_buf = []
            counter = 1
            for line in buf:
                l_strip = line.strip()
                if not l_strip:
                    new_buf.append(line)
                    continue
                m = entry_prefix_re.match(line)
                if m:
                    prefix, separator, text = m.groups()
                    if "." not in separator:
                        separator = ". "
                    # Discard bullet/hyphen prefix
                    new_line = f"{counter}{separator}{text}\n"
                    new_buf.append(new_line)
                    counter += 1
                else:
                    new_buf.append(line)
            return new_buf

        def save_if_valid(name, buf, valid):
            if not valid: return
            if name.lower().strip() in ("table of contents", "index"): return
            sorted_buf = sort_table_buffer(buf)
            renumbered_buf = renumber_table_buffer(sorted_buf)
            clean_name = re.sub(r"[^\w-]", "_", name.lower().strip())
            clean_name = clean_name.replace("_-_", "-").replace("_-", "-").replace("-_", "-")
            clean_name = re.sub(r"_+", "_", clean_name).strip("_-")[:60] or "extracted_table"
            target = out_dir / f"{clean_name}.md"
            idx = 1
            while target.exists():
                target = out_dir / f"{clean_name}_{idx}.md"
                idx += 1
            with open(target, "w", encoding="utf-8") as tf: tf.writelines(renumbered_buf)
            log_event(f"Extracted: {target.name}")

        for line in lines:
            if CANCEL_EVENT.is_set():
                log_event("Stage 4 split operation aborted by user.", "warn")
                return False

            l_strip = line.strip()
            if not l_strip:
                if pending_header:
                    pending_header["lines"].append(line)
                elif curr_table_buf:
                    curr_table_buf.append(line)
                continue
            
            # Skip logic
            if any(r.match(l_strip) for r in self.parser_rules["skips"]): continue
            
            # Header check
            header_match = next((r.match(l_strip) for r in self.parser_rules["headers"]), None)
            if header_match:
                title = header_match.group("title").strip() if "title" in header_match.groupdict() else l_strip
                new_name = re.sub(r"^#+\s*|^[\d\.\-\s]+", "", title)
                
                if pending_header:
                    curr_words = set(re.findall(r'\w+', curr_name.lower()))
                    pend_words = set(re.findall(r'\w+', pending_header["name"].lower()))
                    if not (curr_words and pend_words and (curr_words.issubset(pend_words) or pend_words.issubset(curr_words))):
                        curr_table_buf.extend(pending_header["lines"])
                    pending_header = None
                
                if curr_name == "unnamed" and has_entries:
                    curr_name = new_name
                    curr_table_buf.insert(0, line)
                else:
                    if has_entries:
                        pending_header = {"name": new_name, "lines": [line]}
                    else:
                        curr_name = new_name
                        curr_table_buf.append(line)
                continue
            
            # Entry check (Validation & Slicing)
            entry_match = None
            for r in self.parser_rules["entries"]:
                m = r.match(l_strip)
                if m:
                    entry_match = m
                    break
                    
            if entry_match:
                start_val = None
                if "start" in entry_match.groupdict():
                    start_val = entry_match.group("start")
                if not start_val:
                    try: start_val = entry_match.group(1)
                    except IndexError: pass
                
                if not start_val: continue
                start_num = int(start_val)
                
                end_num_val = entry_match.group("end") if "end" in entry_match.groupdict() else None
                end_num = int(end_num_val) if end_num_val else None
                
                if not (1 <= start_num <= 100 and (end_num is None or end_num <= 100)):
                    continue
                
                has_entries = True
                curr_num = end_num if end_num else start_num
                
                is_reset = False
                if last_entry_num is not None and start_num <= last_entry_num:
                    if pending_header:
                        is_reset = True
                    else:
                        if start_num <= 1 and start_num in seen_entries:
                            is_reset = True
                        
                if is_reset:
                    if curr_table_buf:
                        save_if_valid(curr_name, curr_table_buf, has_entries)
                    
                    if pending_header:
                        curr_name = pending_header["name"]
                        curr_table_buf = pending_header["lines"] + [line]
                        pending_header = None
                    else:
                        curr_name = "unnamed"
                        curr_table_buf = [line]
                        
                    seen_entries = {start_num}
                    last_entry_num = curr_num
                else:
                    if pending_header:
                        curr_words = set(re.findall(r'\w+', curr_name.lower()))
                        pend_words = set(re.findall(r'\w+', pending_header["name"].lower()))
                        if not (curr_words and pend_words and (curr_words.issubset(pend_words) or pend_words.issubset(curr_words))):
                            curr_table_buf.extend(pending_header["lines"])
                        pending_header = None
                        
                    curr_table_buf.append(line)
                    seen_entries.add(start_num)
                    last_entry_num = curr_num
                continue
                
            # Other lines (descriptions / comments / empty lines)
            if pending_header:
                pending_header["lines"].append(line)
            else:
                curr_table_buf.append(line)

        # Flush final buffer
        if pending_header:
            curr_words = set(re.findall(r'\w+', curr_name.lower()))
            pend_words = set(re.findall(r'\w+', pending_header["name"].lower()))
            if not (curr_words and pend_words and (curr_words.issubset(pend_words) or pend_words.issubset(curr_words))):
                curr_table_buf.extend(pending_header["lines"])
        save_if_valid(curr_name, curr_table_buf, has_entries)
        return True

    def run_stage_5_parse(self, stem):
        """Step 5 (Deterministic): Parse extracted markdown tables directly to Foundry VTT JSON.
        No LLM required - uses regex on the clean numbered markdown produced by Step 4.
        Handles single-line and multi-line (continuation) entries."""
        snip_dir = self.output_dir / "data" / "extracted_tables" / stem
        out_dir  = self.output_dir / "data" / "tables" / "individual" / stem
        out_dir.mkdir(parents=True, exist_ok=True)
        snippets = sorted(snip_dir.glob("*.md"))
        if not snippets: return True
        log_event(f"Stage 5 (Parse): {stem} ({len(snippets)} tables)")

        entry_re  = re.compile(r'^\s*(\d+)[.:\s]+(.*)')
        header_re = re.compile(r'^##\s+(.*)')

        def pick_formula(max_n):
            for die in [4, 6, 8, 10, 12, 20, 100]:
                if max_n <= die:
                    return f"1d{die}"
            return f"1d{max_n}"

        for snip in snippets:
            if CANCEL_EVENT.is_set():
                log_event("Stage 5 parse aborted by user.", "warn")
                break

            with open(snip, "r", encoding="utf-8") as f:
                lines = f.readlines()

            table_name   = snip.stem.replace("_", " ").title()
            results      = []
            current_num  = None
            current_text = None

            for line in lines:
                l = line.strip()
                if not l:
                    continue
                hm = header_re.match(l)
                if hm:
                    # Flush any pending entry before a new section header
                    if current_num is not None and current_text:
                        results.append({"num": current_num, "text": current_text.strip()})
                        current_num, current_text = None, None
                    if len(results) == 0:
                        table_name = hm.group(1).strip()
                    continue
                em = entry_re.match(l)
                if em:
                    # Flush previous entry
                    if current_num is not None and current_text:
                        results.append({"num": current_num, "text": current_text.strip()})
                    current_num  = int(em.group(1))
                    current_text = em.group(2).strip()
                else:
                    # Continuation of a multi-line entry
                    if current_text is not None:
                        current_text = current_text + " " + l

            # Flush last entry
            if current_num is not None and current_text:
                results.append({"num": current_num, "text": current_text.strip()})

            out_path = out_dir / f"{snip.stem}.json"
            if not results or not any(r["text"].strip() for r in results):
                reason = "No entries parsed" if not results else "All entries are blank"
                log_event(f"  {reason} in {snip.name} - skipping and deleting old JSON if exists", "warn")
                if out_path.exists():
                    try:
                        out_path.unlink()
                        log_event(f"    Deleted stale JSON: {out_path.name}")
                    except Exception as e:
                        log_event(f"    Failed to delete stale JSON: {e}", "warn")
                continue

            max_n   = max(r["num"] for r in results)
            formula = pick_formula(max_n)

            foundry_res = []
            for idx, r in enumerate(results):
                foundry_res.append({
                    "_id": f"tf{idx:04d}", "type": 0, "text": r["text"],
                    "weight": 1, "range": [r["num"], r["num"]], "drawn": False,
                    "img": "icons/svg/d20-black.svg"
                })

            final = {
                "name": table_name,
                "img": "icons/svg/d20-grey.svg",
                "description": f"Extracted from {stem}.pdf",
                "results": foundry_res,
                "formula": formula,
                "replacement": True, "displayRoll": True,
                "flags": {"jenne-table-forge": {"source": stem}}
            }
            out_path = out_dir / f"{snip.stem}.json"
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(final, f, indent=2)
            log_event(f"  Parsed: {snip.name} -> {len(foundry_res)} entries")
        return True

    def run_stage_5_compile(self, stem):
        snip_dir = self.output_dir / "data" / "extracted_tables" / stem
        out_dir = self.output_dir / "data" / "tables" / "individual" / stem
        out_dir.mkdir(parents=True, exist_ok=True)
        snippets = list(snip_dir.glob("*.md"))
        if not snippets: return True
        log_event(f"Stage 5: Compiling {stem} ({len(snippets)} snippets)")
        prompt = 'Convert RPG Table to JSON. Schema: {"name":"", "formula":"1d100", "results":[{"range":[1,1],"text":""}]}. Output RAW JSON only.'
        for snip in tqdm(snippets, desc=f"LLM: {stem[:20]}", leave=False):
            if CANCEL_EVENT.is_set():
                log_event("Stage 5 compile operation aborted by user.", "warn")
                break

            with open(snip, "r", encoding="utf-8") as f: content = f.read()
            try:
                res = requests.post(f"{self.ollama_url}/api/generate", json={
                    "model": self.ollama_model, "prompt": f"{prompt}\n{content}",
                    "stream": False, "format": "json", "options": {"temperature": 0}
                }, timeout=120)
                data = json.loads(res.json()['response'])
                
                foundry_res = []
                for idx, r in enumerate(data.get("results", [])):
                    foundry_res.append({
                        "_id": f"tf{idx:04d}", "type": 0, "text": r["text"],
                        "weight": 1, "range": r["range"], "drawn": False, "img": "icons/svg/d20-black.svg"
                    })
                
                final = {
                    "name": data.get("name", snip.stem.replace("_", " ").title()),
                    "img": "icons/svg/d20-grey.svg",
                    "description": f"Extracted from {stem}.pdf", "results": foundry_res,
                    "formula": data.get("formula", "1d100"), "replacement": True, "displayRoll": True,
                    "flags": {"jenne-table-forge": {"source": stem}}
                }
                with open(out_dir / f"{snip.stem}.json", "w", encoding="utf-8") as f: json.dump(final, f, indent=2)
            except Exception as e: log_event(f"Snippet Failed {snip.name}: {e}", "error")
        return True

    def run_stage_6_finalize(self):
        """Step 6: Finalize extraction by running mappings, categorization, auto-tagging,
        lock enforcement, warning logging, and manifest compilation."""
        log_event("Stage 6: Finalizing catalog database (mappings, locks, tags, warnings)")
        
        data_dir = self.output_dir / "data"
        tables_dir = data_dir / "tables"
        mappings_file = data_dir / "tableforge_mappings.json"
        warnings_file = data_dir / "pipeline_warnings.json"
        
        # Load mappings file with safe fallbacks
        mappings = {"books": {}, "tables": {}}
        if mappings_file.exists():
            try:
                with open(mappings_file, "r", encoding="utf-8") as f:
                    mappings = json.load(f)
            except Exception as e:
                log_event(f"Error loading mappings file: {e}", "error")
        if "books" not in mappings: mappings["books"] = {}
        if "tables" not in mappings: mappings["tables"] = {}

        # Auto-tagging rules
        AUTO_TAG_RULES = [
            (r"\bnames?\b", ["names"]),
            (r"\bmale\b", ["male"]),
            (r"\bfemale\b", ["female"]),
            (r"\b(orc|elf|elven|dwarf|dwarven|halfling|gnome|drow|human|tiefling|dragonborn|goblin|kobold|undead|demon|devil|angel)\b", lambda m: [m.group(1).lower()]),
            (r"\bquests?\b", ["quest"]),
            (r"\bencounters?\b", ["encounter"]),
            (r"\bloot\b|\bmagic\s+items?\b", ["loot"]),
            (r"\btraps?\b", ["trap"]),
            (r"\bpuzzles?\b", ["puzzle"]),
            (r"\bweather\b", ["weather"]),
            (r"\b(drugs?|potions?|herbs?|spices?|poisons?)\b", ["alchemy"]),
            (r"\b(taverns?|inns?|shops?|stores?|markets?)\b", ["urban"]),
            (r"\b(forest|jungle|swamp|desert|mountain|tundra|sea|aquatic|hills?|plains?)\b", lambda m: [m.group(1).lower()]),
        ]

        # Category mapping rules
        CATEGORY_RULES = [
            (r"\bnames?\b", "names"),
            (r"\bquests?\b", "quest"),
            (r"\bencounters?\b", "encounter"),
            (r"\b(loot|armor|weapons?|items?|magic)\b", "loot"),
            (r"\btraps?\b", "trap"),
            (r"\bpuzzles?\b", "puzzle"),
            (r"\bweather\b", "weather"),
            (r"\b(drugs?|potions?|herbs?|poisons?)\b", "alchemy"),
        ]

        def get_category_and_tags(table_name):
            # Categorize
            category = "misc"
            for pattern, cat in CATEGORY_RULES:
                if re.search(pattern, table_name, re.IGNORECASE):
                    category = cat
                    break
            
            # Tags
            tags = set()
            for pattern, tag_vals in AUTO_TAG_RULES:
                match = re.search(pattern, table_name, re.IGNORECASE)
                if match:
                    if callable(tag_vals):
                        tags.update(tag_vals(match))
                    else:
                        tags.update(tag_vals)
            return category, sorted(list(tags))

        # Scan and finalize individual tables
        indiv_dir = tables_dir / "individual"
        table_manifest = []
        warnings = {"low_entry_count": [], "suspected_metadata": [], "unnamed_tables": []}

        if indiv_dir.exists():
            for pdf_dir in sorted(indiv_dir.iterdir()):
                if not pdf_dir.is_dir():
                    continue
                
                # Book name auto-derivation
                book_key = pdf_dir.name
                if book_key not in mappings["books"]:
                    clean_book_name = book_key.replace("-", " ").title()
                    mappings["books"][book_key] = {
                        "name": clean_book_name
                    }
                book_data = mappings["books"][book_key]
                book_display_name = book_data.get("name", book_key.replace("-", " ").title())

                for table_file in sorted(pdf_dir.glob("*.json")):
                    # Skip .new.json files from previous runs to prevent duplicates/errors
                    if table_file.name.endswith(".new.json"):
                        continue

                    table_key = f"{book_key}/{table_file.stem}"
                    
                    # Delete orphan JSON files that no longer have a source markdown file
                    md_file = data_dir / "extracted_tables" / book_key / f"{table_file.stem}.md"
                    if not md_file.exists():
                        try:
                            table_file.unlink()
                            log_event(f"  Deleted orphan table JSON: {table_key}.json")
                        except Exception as e:
                            log_event(f"  Failed to delete orphan JSON {table_file.name}: {e}", "warn")
                        continue
                    try:
                        with open(table_file, "r", encoding="utf-8") as f:
                            table_data = json.load(f)

                        # Auto-derive defaults if not in mappings
                        orig_name = table_data.get("name", table_file.stem.replace("_", " ").title())
                        category, tags = get_category_and_tags(orig_name)
                        
                        if table_key not in mappings["tables"]:
                            mappings["tables"][table_key] = {
                                "name": orig_name,
                                "category": category,
                                "tags": tags
                            }
                        
                        tbl_map = mappings["tables"][table_key]
                        final_name = tbl_map.get("name", orig_name)
                        final_category = tbl_map.get("category", category)
                        final_tags = tbl_map.get("tags", tags)
                        
                        # Apply cleaned data to the table structure
                        table_data["name"] = final_name
                        table_data["book"] = book_display_name
                        table_data["category"] = final_category
                        table_data["tags"] = final_tags
                        
                        # Formula and roll_type normalization
                        formula = table_data.get("formula", "1d100")
                        roll_type = "custom"
                        fm = re.search(r'1d(\d+)', formula.lower())
                        if fm:
                            roll_type = f"d{fm.group(1)}"
                        
                        table_data["roll_type"] = roll_type
                        table_data["sort_key"] = final_name.lower().strip()
                        table_data["entry_count"] = len(table_data.get("results", []))
                        table_data["version"] = 2
                        
                        # Set flags for VTT
                        if "flags" not in table_data:
                            table_data["flags"] = {}
                        table_data["flags"]["jenne-table-forge"] = {
                            "source": book_key,
                            "book": book_display_name,
                            "category": final_category,
                            "tags": final_tags
                        }

                        with open(table_file, "w", encoding="utf-8") as f:
                            json.dump(table_data, f, indent=2, ensure_ascii=False)

                        rel_path = f"individual/{pdf_dir.name}/{table_file.name}"
                        source_pdf = book_key + ".pdf"
                        
                        # Manifest entry
                        manifest_entry = {
                            "name": final_name,
                            "book": book_display_name,
                            "file": rel_path,
                            "source": source_pdf,
                            "category": final_category,
                            "tags": final_tags,
                            "roll_type": roll_type,
                            "sort_key": final_name.lower().strip(),
                            "is_master": False,
                            "items": len(table_data.get("results", []))
                        }
                        table_manifest.append(manifest_entry)

                        # Warning checks
                        entries_count = len(table_data.get("results", []))
                        if entries_count <= 3:
                            warnings["low_entry_count"].append({
                                "file": rel_path,
                                "name": final_name,
                                "entries": entries_count
                            })
                        
                        metadata_triggers = ["credits", "introduction", "about the author", "title page", "table of contents", "index"]
                        if any(trigger in final_name.lower() for trigger in metadata_triggers):
                            warnings["suspected_metadata"].append({
                                "file": rel_path,
                                "name": final_name,
                                "entries": entries_count
                            })

                        if not final_name or final_name.lower() == "unnamed" or final_name.lower().startswith("unnamed_") or final_name.lower().startswith("unnamed "):
                            warnings["unnamed_tables"].append({
                                "file": rel_path,
                                "name": final_name,
                                "entries": entries_count
                            })

                    except Exception as e:
                        log_event(f"Error finalising table JSON {table_file.name}: {e}", "warn")

        # Scan combined tables (apply clean name / metadata)
        comb_dir = tables_dir / "combined"
        if comb_dir.exists():
            for theme_dir in comb_dir.iterdir():
                if theme_dir.is_dir():
                    for table_file in theme_dir.glob("*.json"):
                        try:
                            with open(table_file, "r", encoding="utf-8") as f:
                                table_data = json.load(f)
                            
                            rel_path = f"combined/{theme_dir.name}/{table_file.name}"
                            source = table_data.get("flags", {}).get("jenne-table-forge", {}).get("source", "Combined Theme")
                            
                            table_manifest.append({
                                "name": table_data.get("name", table_file.stem.replace("_", " ").title()),
                                "book": "Combined Tables",
                                "file": rel_path,
                                "source": source,
                                "category": "combined",
                                "tags": ["combined", theme_dir.name],
                                "roll_type": "custom",
                                "sort_key": table_data.get("name", "").lower(),
                                "is_master": True,
                                "items": len(table_data.get("results", []))
                            })
                        except Exception as e:
                            log_event(f"Failed to read table JSON for manifest {table_file.name}: {e}", "warn")

        # Sort manifest alphabetically by name
        table_manifest.sort(key=lambda t: t["sort_key"])

        # Write manifest
        metadata_file = data_dir / "metadata.json"
        with open(metadata_file, "w", encoding="utf-8") as f:
            json.dump(table_manifest, f, indent=2, ensure_ascii=False)

        # Write warnings log
        with open(warnings_file, "w", encoding="utf-8") as f:
            json.dump(warnings, f, indent=2, ensure_ascii=False)

        # Write mappings file back (appends only, preserves existing)
        with open(mappings_file, "w", encoding="utf-8") as f:
            json.dump(mappings, f, indent=2, ensure_ascii=False)

        log_event(f"Manifest written with {len(table_manifest)} tables.", "success")
        log_event(f"Warnings written: {len(warnings['low_entry_count'])} low entries, {len(warnings['suspected_metadata'])} metadata files, {len(warnings['unnamed_tables'])} unnamed tables.", "warn")

    def process_stem(self, stem, source_path=None, step=None, start_at=None, force=False):
        if self.skip_processed and not force and stem in self.tracker:
            log_event(f"Skipping (Finished): {stem}"); return
        
        log_event(f"--- Processing: {stem} ---")
        steps = [1, 2, 3, 4, 5]
        if step: steps = [step]
        elif start_at: steps = [s for s in steps if s >= start_at]

        if 1 in steps and not CANCEL_EVENT.is_set(): self.run_stage_1_ocr(stem, source_path)
        if 2 in steps and not CANCEL_EVENT.is_set(): self.run_stage_2_flatten(stem, force=force)
        if 3 in steps and not CANCEL_EVENT.is_set(): self.run_stage_3_clean(stem, force=force)
        if 4 in steps and not CANCEL_EVENT.is_set(): self.run_stage_4_split(stem)
        if 5 in steps and not CANCEL_EVENT.is_set():
            if getattr(self, 'use_llm', False):
                self.run_stage_5_compile(stem)
            else:
                self.run_stage_5_parse(stem)

        if (5 in steps or not (step or start_at)) and not CANCEL_EVENT.is_set():
            self.tracker[stem] = {"processed": str(datetime.datetime.now())}
            self.save_tracker()

# --- BACKEND HTTP SERVER FOR VTT INTEGRATION ---

class TableForgeServerHandler(BaseHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        if path == "/status":
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            status_data = {
                "running": IS_RUNNING,
                "progress": GLOBAL_PROGRESS,
                "logs": GLOBAL_LOGS
            }
            self.wfile.write(json.dumps(status_data).encode('utf-8'))

        elif path == "/browse":
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            
            selected_dir = ""
            try:
                import tkinter as tk
                from tkinter import filedialog
                
                def open_picker():
                    nonlocal selected_dir
                    root = tk.Tk()
                    root.withdraw()
                    root.attributes("-topmost", True)
                    selected_dir = filedialog.askdirectory(title="Select RPG PDF Folder")
                    root.destroy()
                
                t = threading.Thread(target=open_picker)
                t.start()
                t.join()
            except Exception as e:
                log_event(f"Failed to open folder picker: {e}", "error")
                
            self.wfile.write(json.dumps({"path": selected_dir}).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        
        if path == "/run":
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length).decode('utf-8')
            try:
                params = json.loads(post_data)
            except:
                params = {}
            
            pdf_dir = params.get("pdf_dir", "")
            
            global IS_RUNNING
            if IS_RUNNING:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"error": "TableForge pipeline is already running!"}).encode('utf-8'))
                return

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "started"}).encode('utf-8'))
            
            # Spawn the pipeline execution in a separate background thread
            threading.Thread(target=run_compilation_pipeline, args=(pdf_dir,)).start()

        elif path == "/kill":
            global CANCEL_EVENT
            CANCEL_EVENT.set()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "killed"}).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

def generate_metadata_json(output_dir):
    extractor = TableForgeExtractor(pdf_dirs=[output_dir], output_dir=output_dir)
    extractor.run_stage_6_finalize()

def run_compilation_pipeline(pdf_dir):
    global IS_RUNNING, GLOBAL_PROGRESS, GLOBAL_LOGS, CANCEL_EVENT
    IS_RUNNING = True
    GLOBAL_PROGRESS = 5
    GLOBAL_LOGS.clear()
    CANCEL_EVENT.clear()
    
    log_event("Starting TableForge Local Pipeline in background thread...", "info")
    log_event(f"Target folder: {pdf_dir}", "info")
    
    try:
        extractor = TableForgeExtractor(pdf_dirs=[pdf_dir], output_dir=DEFAULT_OUTPUT_PATH)
        src = Path(pdf_dir)
        if not src.exists():
            log_event(f"Error: Target directory does not exist: {pdf_dir}", "error")
            return
            
        pdf_files = []
        if src.is_file():
            if src.suffix.lower() == ".pdf":
                pdf_files.append(src)
        else:
            pdf_files = list(src.glob("**/*.pdf"))
            
        if not pdf_files:
            log_event("Error: No PDF files found in search folder!", "error")
            return
            
        log_event(f"Discovered {len(pdf_files)} PDF documents to process.", "info")
        
        for idx, pdf_path in enumerate(pdf_files):
            if CANCEL_EVENT.is_set():
                log_event("Processing cancelled by user command.", "warn")
                break
                
            stem = pdf_path.stem
            log_event(f"Processing PDF ({idx+1}/{len(pdf_files)}): {pdf_path.name}", "info")
            
            extractor.process_stem(stem, source_path=pdf_path, force=False)
            
            GLOBAL_PROGRESS = int(((idx + 1) / len(pdf_files)) * 95)
            
        if CANCEL_EVENT.is_set():
            log_event("Pipeline stopped before completion.", "warn")
        else:
            # Step 6: Generate metadata.json catalog list
            generate_metadata_json(DEFAULT_OUTPUT_PATH)
            GLOBAL_PROGRESS = 100
            log_event("TableForge local processing completed successfully!", "success")
            
    except Exception as e:
        log_event(f"Pipeline crashed: {e}", "error")
    finally:
        IS_RUNNING = False

def run_http_server(port=8055):
    server_address = ('', port)
    try:
        httpd = HTTPServer(server_address, TableForgeServerHandler)
        log_event(f"TableForge Local HTTP Server listening on port {port}...", "success")
        httpd.serve_forever()
    except Exception as e:
        log_event(f"Failed to start HTTP Server: {e}", "error")

def main():
    load_dotenv(Path(__file__).parent / "tableforge.env")
    env_pdf_dir = os.environ.get("PDF_FOLDERS", str(DEFAULT_OUTPUT_PATH))
    parser = argparse.ArgumentParser(prog="tableforge.py", formatter_class=argparse.RawDescriptionHelpFormatter, description="VTT-TableForge Pipeline")
    parser.add_argument("--pdf-dir",  default=env_pdf_dir, help="Folder containing source PDFs.")
    parser.add_argument("--step",     type=int, choices=[1,2,3,4,5,6], help="Run stage N only.")
    parser.add_argument("--start-at", type=int, choices=[1,2,3,4,5,6], help="Begin at stage N.")
    parser.add_argument("--clean",    action="store_true", help="Wipe downstream directories and tracker.")
    parser.add_argument("--force",    action="store_true", help="Ignore tracker results.")
    parser.add_argument("--server",   action="store_true", help="Start local HTTP micro-server on port 8055.")
    parser.add_argument("--llm",      action="store_true", help="Use Ollama LLM for Step 5 (default: deterministic regex parser).")
    args = parser.parse_args()
    
    if args.server:
        run_http_server(8055)
        return

    start_stage = args.step or args.start_at or 1
    extractor = TableForgeExtractor(pdf_dirs=[args.pdf_dir], output_dir=DEFAULT_OUTPUT_PATH)
    extractor.use_llm = getattr(args, 'llm', False)
    
    if start_stage == 6:
        generate_metadata_json(DEFAULT_OUTPUT_PATH)
        return
        
    jobs = []
    if start_stage == 1:
        src = Path(args.pdf_dir)
        if src.is_file():
            if src.suffix.lower() == ".pdf":
                jobs.append((src.stem, src))
        else:
            for p in src.glob("**/*.pdf"): jobs.append((p.stem, p))
    else:
        mapping = {2:("raw_markdown","_raw.md"), 3:("flat_markdown","_flat.md"), 4:("clean_markdown","_clean.md"), 5:("extracted_tables","")}
        f_name, suffix = mapping[start_stage]
        s_dir = DEFAULT_OUTPUT_PATH / "data" / f_name
        if s_dir.exists():
            if start_stage == 5:
                for d in s_dir.iterdir(): 
                    if d.is_dir(): jobs.append((d.name, None))
            else:
                for f in s_dir.glob(f"*{suffix}"): jobs.append((f.name.replace(suffix, ""), None))

    if args.clean:
        folders = {1:"raw_markdown", 2:"flat_markdown", 3:"clean_markdown", 4:"extracted_tables", 5:"tables/individual"}
        for s in range(start_stage, 6):
            p = DEFAULT_OUTPUT_PATH / "data" / folders[s]
            if p.exists(): shutil.rmtree(p, ignore_errors=True)
        for stem, _ in jobs:
            if stem in extractor.tracker: del extractor.tracker[stem]
        extractor.save_tracker()

    print(f"[INFO] Discovered {len(jobs)} items to process.")
    for stem, path in jobs:
        force_run = bool(args.step or args.start_at or args.force or args.clean)
        extractor.process_stem(stem, source_path=path, step=args.step, start_at=args.start_at, force=force_run)
    
    # Regenerate manifest
    generate_metadata_json(DEFAULT_OUTPUT_PATH)

if __name__ == "__main__":
    main()