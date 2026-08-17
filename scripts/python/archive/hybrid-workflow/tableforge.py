import os
os.environ["DOCLING_DEVICE"] = "cpu"
os.environ["DOCLING_NUM_THREADS"] = "4"
import re
import time
import json
import argparse
import datetime
from pathlib import Path
from tqdm import tqdm
import pdfplumber
from pypdf import PdfReader
from concurrent.futures import ThreadPoolExecutor
import logging

# Suppress noisy layout/OCR/Docling dependency loggers
logging.basicConfig(level=logging.WARNING, force=True)
logging.getLogger("RapidOCR").setLevel(logging.WARNING)
logging.getLogger("docling").setLevel(logging.WARNING)
logging.getLogger("pdfminer").setLevel(logging.WARNING)
logging.getLogger("onnxruntime").setLevel(logging.WARNING)

# Optional Gemini API Import
try:
    from google import genai
    HAS_NEW_GENAI = True
    HAS_GEMINI = True
except ImportError:
    HAS_NEW_GENAI = False
    try:
        import google.generativeai as old_genai
        HAS_GEMINI = True
    except ImportError:
        HAS_GEMINI = False

# Optional Docling Import
try:
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.datamodel.base_models import InputFormat
    HAS_DOCLING = True
except ImportError:
    HAS_DOCLING = False

# Load dotenv if available
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / "tableforge.env")
    HAS_DOTENV = True
except ImportError:
    HAS_DOTENV = False

# Default configurations
DEFAULT_OUTPUT_PATH = str(Path(__file__).parent.parent.parent.resolve())
DEFAULT_PDF_DIR = r"d:\OneDrive\DnD\Reference Documents\Random Tables\Tables"
CONFIG_FILE = str(Path(__file__).parent / "weight_config.json")
TRACKER_FILE = str(Path(__file__).parent / "tableforge_tracker.json")

class TableForgeExtractor:
    def __init__(self, pdf_dirs, output_dir, api_key=None, skip_processed=False, paid_tier=False):
        import os
        from pathlib import Path
        self.pdf_dirs = [Path(d) for d in pdf_dirs]
        self.output_dir = Path(output_dir)
        self.api_key = api_key
        self.skip_processed = skip_processed
        self.paid_tier = paid_tier
        
        self.tracker = {}
        self.load_tracker()
        self.load_config()
        
        # Determine active LLM mode
        self.gemini_enabled = bool(self.api_key)
        self.ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434").strip()
        self.ollama_model = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b").strip()
        self.ollama_enabled = bool(os.environ.get("LLM_PROVIDER") == "ollama")
        
        # Load parser rules
        rules_path = Path(__file__).parent / "parser_rules.md"
        self.parser_rules = self.load_parser_rules(rules_path)
        
        # Initialize docling converter
        try:
            from docling.document_converter import DocumentConverter
            self.doc_converter = DocumentConverter()
            print("[OK] IBM Docling document converter initialized successfully.")
        except Exception as e:
            self.doc_converter = None
            print(f"[WARN] Failed to initialize Docling: {e}. Heuristic backup will be used.")
            
        # Initialize Gemini API Client
        if self.gemini_enabled and not self.ollama_enabled:
            try:
                from google import genai
                self.new_genai_client = genai.Client(api_key=self.api_key)
                self.model_name = "gemini-2.5-flash"
                print(f"[OK] Paid Google GenAI Client initialized using model '{self.model_name}'.")
            except Exception:
                import google.generativeai as generativeai
                generativeai.configure(api_key=self.api_key)
                self.new_genai_client = None
                self.old_genai_model = generativeai.GenerativeModel("gemini-1.5-flash")
                print(f"[OK] Legacy Google GenerativeAI Client initialized.")
        elif self.ollama_enabled:
            print(f"[OK] Local Ollama LLM provider active using model '{self.ollama_model}' at '{self.ollama_url}'.")
        else:
            print("[WARN] Running in local heuristics (No-LLM) fallback mode.")

    def load_parser_rules(self, rules_path):
        import re
        rules = {"headers": [], "entries": [], "skips": []}
        if not rules_path.exists():
            print(f"  [WARN] Parser rules file not found: {rules_path}. Using default patterns.")
            rules["headers"] = [re.compile(r"^\s*#+\s+(?P<title>.*)"), re.compile(r"^\s*(?P<title>.*Table\s*#?\s*\d+.*)")]
            rules["entries"] = [re.compile(r"^\s*\|?\s*(?P<start>\d+)(?:\s*-\s*(?P<end>\d+))?[\.\s\|]+(?P<text>.*)")]
            rules["skips"] = [re.compile(r"^\s*<!--.*-->\s*$")]
            return rules

        current_section = None
        with open(rules_path, "r", encoding="utf-8") as f:
            for line in f:
                line_clean = line.strip()
                if line_clean.startswith("## Header Patterns"):
                    current_section = "headers"
                elif line_clean.startswith("## Entry Patterns"):
                    current_section = "entries"
                elif line_clean.startswith("## Skip Patterns"):
                    current_section = "skips"
                elif line_clean.startswith("* `") and line_clean.endswith("`") and current_section:
                    pattern = line_clean.split("`")[1]
                    rules[current_section].append(re.compile(pattern, re.IGNORECASE))
        return rules

    def load_config(self):
        import json
        config_path = Path(__file__).parent / "weight_config.json"
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                self.weights_config = json.load(f)
        else:
            self.weights_config = {
                "weight_profiles": {
                    "rarity": {"common": 1.0, "uncommon": 0.5, "rare": 0.25}
                }
            }

    def load_tracker(self):
        import json
        tracker_path = Path(__file__).parent / "tableforge_tracker.json"
        if tracker_path.exists():
            with open(tracker_path, "r", encoding="utf-8") as f:
                self.tracker = json.load(f)
        else:
            self.tracker = {}

    def save_tracker(self):
        import json
        tracker_path = Path(__file__).parent / "tableforge_tracker.json"
        with open(tracker_path, "w", encoding="utf-8") as f:
            json.dump(self.tracker, f, indent=2, ensure_ascii=False)

    def clean_text(self, text):
        if not text:
            return ""
        return text.replace("\u2019", "'").replace("\u201d", '"').replace("\u201c", '"').strip()

    def chunk_markdown(self, markdown_text, max_chunk_size=12000, overlap_lines=3):
        lines = markdown_text.split("\n")
        chunks = []
        current_chunk = []
        current_size = 0
        for line in lines:
            line_size = len(line)
            if current_size + line_size > max_chunk_size and current_chunk:
                chunks.append("\n".join(current_chunk))
                current_chunk = current_chunk[-overlap_lines:] if len(current_chunk) >= overlap_lines else current_chunk
                current_size = sum(len(l) for l in current_chunk) + len(current_chunk)
            current_chunk.append(line)
            current_size += line_size + 1
        if current_chunk:
            chunks.append("\n".join(current_chunk))
        return chunks

    def run_stage_1_ocr(self, pdf_path):
        pdf_path = Path(pdf_path)
        raw_md_dir = self.output_dir / "data" / "raw_markdown"
        raw_md_dir.mkdir(parents=True, exist_ok=True)
        raw_md_path = raw_md_dir / f"{pdf_path.stem}_raw.md"
        
        if raw_md_path.exists():
            print(f"  [INFO] Raw markdown already exists: {raw_md_path.name}. Skipping Step 1.")
            return True
            
        print(f"  [INFO] Step 1: Converting PDF to raw layout markdown using IBM Docling...")
        try:
            from pypdf import PdfWriter, PdfReader
            reader = PdfReader(pdf_path)
            total_pages = len(reader.pages)
            
            markdown_parts = []
            batch_size = 15
            for start_idx in range(0, total_pages, batch_size):
                end_idx = min(start_idx + batch_size, total_pages)
                writer = PdfWriter()
                for p_idx in range(start_idx, end_idx):
                    writer.add_page(reader.pages[p_idx])
                    
                temp_pdf_path = pdf_path.parent / f"temp_{pdf_path.stem}_batch_{start_idx}.pdf"
                with open(temp_pdf_path, "wb") as f_out:
                    writer.write(f_out)
                    
                try:
                    print(f"    Batch: pages {start_idx+1} to {end_idx}...")
                    doc_result = self.doc_converter.convert(temp_pdf_path)
                    part_md = doc_result.document.export_to_markdown()
                    tagged_md = f"\n\n<!-- START_PAGE_RANGE_{start_idx+1}_TO_{end_idx} -->\n{part_md}\n<!-- END_PAGE_RANGE_{start_idx+1}_TO_{end_idx} -->\n\n"
                    markdown_parts.append(tagged_md)
                finally:
                    if temp_pdf_path.exists():
                        try:
                            temp_pdf_path.unlink()
                        except:
                            pass
                            
            full_md_text = "\n\n".join(markdown_parts)
            with open(raw_md_path, "w", encoding="utf-8") as f_md:
                f_md.write(full_md_text)
            print(f"  [SUCCESS] Raw markdown saved to: {raw_md_path}")
            return True
        except Exception as e:
            print(f"  [ERROR] Step 1 Layout Extraction failed for {pdf_path.name}: {e}")
            return False

    def run_stage_2_flatten(self, pdf_path):
        """
        Step 2: Column Flattener (Pure Python State Machine)
        -------------------------------------------------------
        Reads the raw OCR markdown (_raw.md) produced by Step 1 and converts
        any multi-column pipe-table structures (2-column, 3-column, etc.) that
        Docling emits for wide D&D random tables into clean, single-column
        numbered lists.  The result is saved as _flat.md.

        This runs entirely in Python — no LLM involved — so it is fast and
        deterministic.  It resolves the chunking-boundary problem: because the
        whole file is processed in one sequential pass, a table that would have
        been split across two LLM chunks is always handled correctly.

        Algorithm
        ---------
        1. Scan lines one at a time.
        2. When a run of pipe-table rows is detected (lines matching
           ``|...|...|``), collect them into a buffer.
        3. On exit from the table run, inspect how many pipe-delimited cells
           each row has.
           - 1 cell  → already a single-column list; emit as-is.
           - 2 cells → left column first, then right column.
           - 3 cells → left, middle, right.
           Cells are stripped, separator rows (``|---|``) are discarded.
        4. Each non-empty cell is emitted as its own line.
        5. Non-table lines are passed through unchanged.
        """
        import re
        pdf_path = Path(pdf_path)
        raw_md_dir = self.output_dir / "data" / "raw_markdown"
        flat_md_dir = self.output_dir / "data" / "flat_markdown"
        flat_md_dir.mkdir(parents=True, exist_ok=True)

        raw_md_path = raw_md_dir / f"{pdf_path.stem}_raw.md"
        flat_md_path = flat_md_dir / f"{pdf_path.stem}_flat.md"

        if flat_md_path.exists():
            print(f"  [INFO] Flat markdown already exists: {flat_md_path.name}. Skipping Step 2.")
            return True

        if not raw_md_path.exists():
            print(f"  [ERROR] Raw markdown not found: {raw_md_path}. Run Step 1 first.")
            return False

        print(f"  [INFO] Step 2: Flattening multi-column pipe tables (state machine)...")

        # Regex: a pipe-table content row — starts and ends with | and has at
        # least one interior |.
        pipe_row_re = re.compile(r'^\|(.+\|)+\s*$')
        # Separator rows like |---|---| should be discarded
        sep_row_re = re.compile(r'^\|[\s\-:]+\|[\s\-:|]*$')

        with open(raw_md_path, "r", encoding="utf-8") as f:
            raw_lines = f.readlines()

        out_lines = []
        table_buf = []   # accumulates pipe rows for the current table block

        def flush_table(buf):
            """Convert buffered pipe-table rows to single-column lines."""
            if not buf:
                return []
            result = []
            # Determine column count from first non-separator row
            col_count = 1
            for row in buf:
                if not sep_row_re.match(row.strip()):
                    cells = [c.strip() for c in row.strip().strip('|').split('|')]
                    col_count = len(cells)
                    break

            if col_count <= 1:
                # Single-column table — emit cells as plain lines
                for row in buf:
                    if sep_row_re.match(row.strip()):
                        continue
                    cells = [c.strip() for c in row.strip().strip('|').split('|')]
                    for cell in cells:
                        if cell:
                            result.append(cell + '\n')
                return result

            # Multi-column: collect all rows per column index
            columns = [[] for _ in range(col_count)]
            for row in buf:
                if sep_row_re.match(row.strip()):
                    continue
                cells = [c.strip() for c in row.strip().strip('|').split('|')]
                # Pad to col_count if row is short
                while len(cells) < col_count:
                    cells.append('')
                for ci in range(col_count):
                    if cells[ci]:
                        columns[ci].append(cells[ci])

            # Emit column 0 first, then column 1, then column 2, etc.
            for col in columns:
                for cell in col:
                    result.append(cell + '\n')
            return result

        for line in raw_lines:
            stripped = line.strip()
            if pipe_row_re.match(stripped):
                table_buf.append(line)
            else:
                if table_buf:
                    out_lines.extend(flush_table(table_buf))
                    table_buf = []
                    # Emit a blank line after the flattened block if the current
                    # line is not already blank
                    if stripped:
                        out_lines.append('\n')
                out_lines.append(line)

        # Flush any trailing table
        if table_buf:
            out_lines.extend(flush_table(table_buf))

        with open(flat_md_path, "w", encoding="utf-8") as f_flat:
            f_flat.writelines(out_lines)
        print(f"  [SUCCESS] Flat markdown saved to: {flat_md_path}")
        return True

    def run_stage_3_clean(self, pdf_path):
        pdf_path = Path(pdf_path)
        flat_md_dir = self.output_dir / "data" / "flat_markdown"
        clean_md_dir = self.output_dir / "data" / "clean_markdown"
        clean_md_dir.mkdir(parents=True, exist_ok=True)

        flat_md_path = flat_md_dir / f"{pdf_path.stem}_flat.md"
        clean_md_path = clean_md_dir / f"{pdf_path.stem}_clean.md"

        if clean_md_path.exists():
            print(f"  [INFO] Clean markdown already exists: {clean_md_path.name}. Skipping Step 3.")
            return True

        if not flat_md_path.exists():
            print(f"  [ERROR] Flat markdown not found: {flat_md_path}. Run Step 2 first.")
            return False

        print(f"  [INFO] Step 3: Cleaning OCR text typos and anomalies via LLM...")
        with open(flat_md_path, "r", encoding="utf-8") as f:
            raw_text = f.read()
            
        chunks = self.chunk_markdown(raw_text, max_chunk_size=6000 if self.ollama_enabled else 12000)
        cleaned_parts = []
        
        prompt = """
        You are a document cleaning assistant. Clean the pre-processed Markdown text to standardize it for a state-machine parser.
        Note: Multi-column pipe tables have already been flattened into single-column lists by a prior step.
        Your job is only to fix remaining OCR artifacts.

        Rules:
        1. Fix OCR word-splitting bugs: Remove extra spaces inserted inside words (e.g. 'Saff   ord' -> 'Safford', 'Offi  ce' -> 'Office', 'st  uffed' -> 'stuffed'). Correct split hyphens (e.g. 'half- elf' -> 'half-elf').
        2. Remove Noise: Strip out all page numbers, page headers, page footers, website URLs, and author bylines (e.g. 'Matt Davids', 'www.dicegeeks.com', bare numbers on their own line) that interrupt table entries.
        3. Standardize Spacing: Ensure every table starts with '## Table Name'. Put exactly one blank line after the header, and ensure there are NO empty lines between items in the list.
        4. Deduplicate: If an identical numbered entry appears more than once in the same table, keep only the first occurrence.
        5. Do NOT reorder entries, add entries, change entry text beyond fixing OCR bugs, or restructure lists.

        Return ONLY the cleaned Markdown text. Do not wrap in markdown code blocks or add introductory/concluding text.
        """
        
        for idx, chunk in enumerate(chunks):
            print(f"    Cleaning chunk {idx+1}/{len(chunks)}...")
            try:
                if self.ollama_enabled:
                    import urllib.request
                    import json
                    chat_url = f"{self.ollama_url}/api/chat"
                    payload = {
                        "model": self.ollama_model,
                        "messages": [
                            {"role": "system", "content": prompt},
                            {"role": "user", "content": chunk}
                        ],
                        "options": {"temperature": 0.0, "num_ctx": 32768, "num_predict": -1},
                        "stream": False
                    }
                    req = urllib.request.Request(
                        chat_url,
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"}
                    )
                    with urllib.request.urlopen(req, timeout=120) as raw_response:
                        res_data = json.loads(raw_response.read().decode("utf-8"))
                        cleaned_chunk = res_data["message"]["content"].strip()
                else:
                    if self.new_genai_client:
                        response = self.new_genai_client.models.generate_content(
                            model=self.model_name,
                            contents=[prompt, chunk]
                        )
                    else:
                        response = self.old_genai_model.generate_content([prompt, chunk])
                    cleaned_chunk = response.text.strip()
                cleaned_parts.append(cleaned_chunk)
            except Exception as e:
                print(f"    [WARN] Failed to clean chunk {idx+1}: {e}. Using raw text fallback.")
                cleaned_parts.append(chunk)
                
        cleaned_text = "\n\n".join(cleaned_parts)
        with open(clean_md_path, "w", encoding="utf-8") as f_clean:
            f_clean.write(cleaned_text)
        print(f"  [SUCCESS] Clean markdown saved to: {clean_md_path}")
        return True

    def run_stage_4_split(self, pdf_path):
        import re
        pdf_path = Path(pdf_path)
        clean_md_dir = self.output_dir / "data" / "clean_markdown"
        extracted_tables_dir = self.output_dir / "data" / "extracted_tables" / pdf_path.stem
        extracted_tables_dir.mkdir(parents=True, exist_ok=True)
        
        clean_md_path = clean_md_dir / f"{pdf_path.stem}_clean.md"
        log_path = self.output_dir / "data" / "extracted_tables" / f"{pdf_path.stem}_parse.log"
        
        if not clean_md_path.exists():
            print(f"  [ERROR] Clean markdown not found: {clean_md_path}. Run Step 2 first.")
            return False
            
        print(f"  [INFO] Step 4: Slicing tables with parser rules state-machine...")
        
        # Clear existing md files in the extracted_tables folder
        for old_f in extracted_tables_dir.glob("*.md"):
            try:
                old_f.unlink()
            except:
                pass
                
        with open(clean_md_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            
        log_entries = []
        def log_event(msg):
            print(f"    {msg}")
            log_entries.append(msg)
            
        current_table_lines = []
        current_table_name = None
        in_table = False
        has_header = False
        
        headers_compiled = self.parser_rules["headers"]
        entries_compiled = self.parser_rules["entries"]
        skips_compiled = self.parser_rules["skips"]
        
        def save_accumulated_table():
            nonlocal current_table_lines, current_table_name
            if len(current_table_lines) > 2:
                # Sanitize table name for filename
                clean_name = re.sub(r"[^\w-]", "_", current_table_name.lower().strip())
                clean_name = re.sub(r"_+", "_", clean_name).strip("_")
                if not clean_name:
                    clean_name = "extracted_table"
                    
                target_md_path = extracted_tables_dir / f"{clean_name}.md"
                if target_md_path.exists():
                    idx = 1
                    while (extracted_tables_dir / f"{clean_name}_{idx}.md").exists():
                        idx += 1
                    target_md_path = extracted_tables_dir / f"{clean_name}_{idx}.md"
                    
                with open(target_md_path, "w", encoding="utf-8") as f_tbl:
                    f_tbl.write("".join(current_table_lines))
                log_event(f"Saved table '{current_table_name}' to: {target_md_path.name}")
            current_table_lines = []
            current_table_name = None
            
        for line_idx, line in enumerate(lines):
            line_clean = line.strip()
            if not line_clean:
                if in_table:
                    current_table_lines.append(line)
                continue
                
            is_skip = False
            for r in skips_compiled:
                if r.match(line_clean):
                    is_skip = True
                    break
            if is_skip:
                continue
                
            header_match = None
            for r in headers_compiled:
                m = r.match(line_clean)
                if m:
                    header_match = m
                    break
                    
            if header_match:
                if in_table or len(current_table_lines) > 0:
                    save_accumulated_table()
                
                title = header_match.group("title").strip() if "title" in header_match.groupdict() else line_clean
                title = re.sub(r"^#+\s*", "", title)
                title = re.sub(r"^[\d\.\-\s]+", "", title)
                current_table_name = title
                current_table_lines = [line]
                in_table = False
                has_header = True
                log_event(f"Line {line_idx+1}: Trigger Table Header '{title}'")
                continue
                
            is_entry = False
            for r in entries_compiled:
                if r.match(line_clean):
                    is_entry = True
                    break
                    
            if has_header and not in_table:
                if is_entry:
                    in_table = True
                    log_event(f"Line {line_idx+1}: Trigger First Entry Row '{line_clean[:40]}'")
                current_table_lines.append(line)
                continue
                
            if in_table:
                if is_entry:
                    current_table_lines.append(line)
                else:
                    current_table_lines.append(line)
                    
        save_accumulated_table()
        
        with open(log_path, "w", encoding="utf-8") as f_log:
            f_log.write("\n".join(log_entries))
        print(f"  [SUCCESS] Table slicing completed. Saved logs to: {log_path}")
        return True

    def run_stage_5_compile(self, pdf_path):
        import re
        import json
        pdf_path = Path(pdf_path)
        extracted_tables_dir = self.output_dir / "data" / "extracted_tables" / pdf_path.stem
        output_tables_dir = self.output_dir / "data" / "tables" / "individual" / pdf_path.stem
        output_tables_dir.mkdir(parents=True, exist_ok=True)
        
        for old_f in output_tables_dir.glob("*.json"):
            try:
                old_f.unlink()
            except:
                pass
                
        md_files = list(extracted_tables_dir.glob("*.md"))
        if not md_files:
            print(f"  [INFO] No table markdown files found in: {extracted_tables_dir}. Run Step 3 first.")
            return True
            
        print(f"  [INFO] Step 4: Compiling {len(md_files)} markdown tables to VTT JSON via LLM...")
        
        prompt_template = """
        You are an expert utility designed to convert RPG table Markdown text into a structured JSON rollable table.
        Clean up misspelling, correct spacing errors, identify range rolls, and output the final VTT structure.
        
        Rules:
        1. Formulate the correct rolling formula (e.g. "1d100", "1d20").
        2. Assign a "tags" array inside the flags structure containing keywords describing the table (e.g. ["names", "male", "human"] or ["items", "magic", "loot"]).
        3. Every individual roll number or range in the source document must map to exactly one separate result object in the "results" array. Do NOT group multiple roll numbers or multiple different outcomes together into a single comma-separated text entry. Every single roll outcome must have its own distinct results object.
        4. Capture any introductory context paragraphs, succeeding rules, modifier tables/details, combat/travel calculations, or explanatory text printed immediately before or after the table, and combine them into a single, cohesive, cleaned text block under a "description" field.
        5. Identify metadata for each entry (e.g. if the entry is a magic item, look for keywords like "common", "uncommon", "rare", "very rare", "legendary", "artifact" and output it under metadata).
        
        Output format MUST be a strict, valid JSON object matching this schema:
        {{
          "name": "Clean Table Name",
          "formula": "1d100",
          "description": "Rules, encounter complications, pace adjustments, etc.",
          "results": [
            {{
              "range": [1, 5],
              "text": "Full cleaned description of the result",
              "weight": 5,
              "metadata": {{
                "rarity": "common",
                "type": "weapon"
              }}
            }}
          ],
          "flags": {{
            "tags": ["tag1", "tag2"]
          }}
        }}
        
        Do not wrap the JSON in ```json markdown code blocks. Output ONLY raw, parseable JSON.
        """
        
        compiled_count = 0
        log_path = self.output_dir / "data" / "extracted_tables" / f"{pdf_path.stem}_parse.log"
        validation_errors = []
        
        for idx, f in enumerate(md_files):
            print(f"    Compiling table {idx+1}/{len(md_files)}: {f.name}...")
            with open(f, "r", encoding="utf-8") as f_in:
                table_md = f_in.read()
                
            try:
                if self.ollama_enabled:
                    import urllib.request
                    chat_url = f"{self.ollama_url}/api/chat"
                    payload = {
                        "model": self.ollama_model,
                        "messages": [
                            {"role": "system", "content": prompt_template},
                            {"role": "user", "content": f"Source Table Markdown:\n{table_md}"}
                        ],
                        "options": {"temperature": 0.0, "num_ctx": 32768, "num_predict": -1},
                        "format": "json",
                        "stream": False
                    }
                    req = urllib.request.Request(
                        chat_url,
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"}
                    )
                    with urllib.request.urlopen(req, timeout=120) as raw_response:
                        res_data = json.loads(raw_response.read().decode("utf-8"))
                        response_text = res_data["message"]["content"].strip()
                else:
                    if self.new_genai_client:
                        response = self.new_genai_client.models.generate_content(
                            model=self.model_name,
                            contents=[prompt_template, f"Source Table Markdown:\n{table_md}"]
                        )
                    else:
                        response = self.old_genai_model.generate_content([prompt_template, f"Source Table Markdown:\n{table_md}"])
                    response_text = response.text.strip()
                    
                if response_text.startswith("```json"):
                    response_text = response_text[7:]
                if response_text.endswith("```"):
                    response_text = response_text[:-3]
                response_text = response_text.strip()
                
                table_data = json.loads(response_text)
                
                # Sanity warnings
                results = table_data.get("results", [])
                if not results:
                    validation_errors.append(f"  [WARN] Table '{f.name}' has no results extracted!")
                else:
                    try:
                        max_val = results[-1]["range"][-1]
                        if len(results) > max_val:
                            validation_errors.append(f"  [WARN] Table '{f.name}' has {len(results)} results which exceeds max roll range {max_val}!")
                    except:
                        pass
                
                description_text = table_data.get("description", "").strip()
                if description_text:
                    full_desc = f"{description_text}\n\n[Extracted from {pdf_path.name}]"
                else:
                    full_desc = f"Extracted from {pdf_path.name}"
                    
                foundry_results = []
                for r_idx, r in enumerate(results):
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
                    
                formula = table_data.get("formula", "1d100")
                tags = table_data.get("flags", {}).get("tags", [])
                
                name_lower = table_data.get("name", "").lower()
                if "name" in name_lower and "names" not in tags:
                    tags.append("names")
                if "male" in name_lower and "male" not in tags:
                    tags.append("male")
                if "female" in name_lower and "female" not in tags:
                    tags.append("female")
                if any(x in name_lower for x in ["loot", "item", "treasure"]):
                    if "loot" not in tags:
                        tags.append("loot")
                if "encounter" in name_lower and "encounters" not in tags:
                    tags.append("encounters")
                    
                foundry_table = {
                    "_id": f"tftable{idx:09d}",
                    "name": table_data.get("name", f.stem.replace("_", " ").title()),
                    "img": "icons/svg/d20-grey.svg",
                    "description": full_desc,
                    "results": foundry_results,
                    "formula": formula,
                    "replacement": True,
                    "displayRoll": True,
                    "folder": None,
                    "flags": {
                        "jenne-table-forge-importer": {
                            "source": pdf_path.name,
                            "tags": tags,
                            "is_master": False,
                            "merged_sources": []
                        }
                    }
                }
                
                json_path = output_tables_dir / f"{f.stem}.json"
                with open(json_path, "w", encoding="utf-8") as f_out:
                    json.dump(foundry_table, f_out, indent=2, ensure_ascii=False)
                compiled_count += 1
            except Exception as e:
                print(f"    [WARN] Failed to compile table {f.name}: {e}")
                validation_errors.append(f"  [ERROR] Table '{f.name}' failed compilation: {e}")
                
        if validation_errors:
            print("\nValidation Warnings:")
            for err in validation_errors:
                print(err)
            with open(log_path, "a", encoding="utf-8") as f_log:
                f_log.write("\n\n=== Validation Checks ===\n" + "\n".join(validation_errors))
                
        print(f"  [SUCCESS] Successfully compiled {compiled_count} JSON tables to: {output_tables_dir}")
        return True

    def extract_pdf_tables(self, pdf_path, step=None, start_at=None):
        pdf_path = Path(pdf_path)
        print(f"\n==========================================================================")
        print(f">>> PROCESSING: {pdf_path.name}")
        print(f"==========================================================================")
        
        # Determine steps to run
        # Step 1: OCR  (Docling PDF → _raw.md)
        # Step 2: Flatten  (Python state machine: multi-column → _flat.md)
        # Step 3: Clean  (LLM OCR typo fix: _flat.md → _clean.md)
        # Step 4: Split  (state machine slicer: _clean.md → individual table .md)
        # Step 5: Compile  (LLM or heuristic: table .md → .json VTT)
        steps = [1, 2, 3, 4, 5]
        if step:
            steps = [step]
        elif start_at:
            steps = [s for s in [1, 2, 3, 4, 5] if s >= start_at]

        success = True

        if 1 in steps:
            success = self.run_stage_1_ocr(pdf_path)
            if not success:
                return []

        if 2 in steps:
            success = self.run_stage_2_flatten(pdf_path)
            if not success:
                return []

        if 3 in steps:
            success = self.run_stage_3_clean(pdf_path)
            if not success:
                return []

        if 4 in steps:
            success = self.run_stage_4_split(pdf_path)
            if not success:
                return []

        if 5 in steps:
            success = self.run_stage_5_compile(pdf_path)
            if not success:
                return []
                
        # Load tables compiled in Step 4 to return context for module gen
        tables = []
        output_tables_dir = self.output_dir / "data" / "tables" / "individual" / pdf_path.stem
        if output_tables_dir.exists():
            import json
            for f in output_tables_dir.glob("*.json"):
                try:
                    with open(f, "r", encoding="utf-8") as f_tbl:
                        tables.append(json.load(f_tbl))
                except:
                    pass
        return tables

    def clean_title_words(self, title):
        import re
        title = title.lower()
        title = re.sub(r"\d+", "", title)
        title = re.sub(r"'s\b", "", title)
        title = re.sub(r"\b(table|book|of|random|fantasy|the|d\d+|\bpart\b|\b#\b|human)\b", "", title, flags=re.IGNORECASE)
        title = re.sub(r"[^\w\s]", "", title).strip()
        words = []
        for w in title.split():
            if len(w) > 4 and w.endswith("s"):
                w = w[:-1]
            words.append(w)
        return set(words)

    def are_titles_similar(self, title_a, title_b, pdf_subject=None):
        words_a = self.clean_title_words(title_a)
        words_b = self.clean_title_words(title_b)
        
        if not words_a or not words_b:
            return False
            
        contrasting_pairs = [
            ("male", "female"), ("melee", "spell"), ("success", "fail"), ("fails", "success"),
            ("dead", "living"), ("forest", "swamp"), ("forest", "desert"), ("forest", "mountain"),
            ("desert", "swamp"), ("desert", "mountain"), ("swamp", "mountain")
        ]
        for w1, w2 in contrasting_pairs:
            if (w1 in words_a and w2 in words_b) or (w2 in words_a and w1 in words_b):
                return False
                
        if words_a == words_b:
            return True
            
        if pdf_subject:
            pdf_words = self.clean_title_words(pdf_subject)
            shared = words_a.intersection(words_b)
            if shared and any(w in pdf_words for w in shared):
                return True
                
        intersection = words_a.intersection(words_b)
        ratio = len(intersection) / max(len(words_a), len(words_b), 1)
        if ratio >= 0.70:
            return True
            
        return False

    def consolidate_pdf_tables(self, tables, pdf_path):
        # We return the tables directly under tag-based matching, but we consolidate duplicates from chunk merges if any
        return tables

    def consolidate_tables(self, tables):
        # Consolidates list of tables
        return tables
    def generate_vtt_module(self, tables):
        """Creates the fully-formed FoundryVTT module structure on disk."""
        print(f"\nGenerating FoundryVTT Module at: {self.output_dir}")
        
        self.output_dir.mkdir(parents=True, exist_ok=True)
        scripts_dir = self.output_dir / "scripts"
        data_dir = self.output_dir / "data"
        tables_dir = data_dir / "tables"
        
        scripts_dir.mkdir(exist_ok=True)
        data_dir.mkdir(exist_ok=True)
        tables_dir.mkdir(exist_ok=True)
        
        module_manifest = {
            "id": "jenne-table-forge-importer",
            "title": "VTT TableForge: Extracted D&D Tables",
            "description": f"Custom rollable tables parsed and semantically consolidated from your PDFs. Contains {len(tables)} tables.",
            "version": "1.0.0",
            "compatibility": {
                "minimum": "12",
                "verified": "14"
            },
            "esmodules": ["scripts/importer.js"],
            "styles": ["styles.css"]
        }
        with open(self.output_dir / "module.json", "w", encoding="utf-8") as f:
            json.dump(module_manifest, f, indent=2, ensure_ascii=False)
            
        table_manifest = []
        for idx, table in enumerate(tables):
            if not isinstance(table, dict) or "name" not in table or not table.get("source_pdf"):
                print(f"  [WARN] Skipping malformed table entry at index {idx}: {table}")
                continue
            is_master = table.get("is_master", False)
            clean_table_name = re.sub(r"[^\w-]", "_", table["name"].lower().strip())
            clean_table_name = re.sub(r"_+", "_", clean_table_name).strip("_")
            
            pdf_folder = re.sub(r"[^\w-]", "_", table["source_pdf"].lower().replace(".pdf", "").strip())
            pdf_folder = re.sub(r"_+", "_", pdf_folder).strip("_")
            
            theme = self.classify_table_theme(table["name"])
            
            page = table.get("page_number", 1)
            if is_master:
                sub_dir = tables_dir / "combined" / theme
                filename = f"master_{clean_table_name}_combined.json"
                rel_path = f"combined/{theme}/{filename}"
            else:
                sub_dir = tables_dir / "individual" / pdf_folder
                filename = f"{clean_table_name}_p{page}.json"
                rel_path = f"individual/{pdf_folder}/{filename}"
                
            sub_dir.mkdir(parents=True, exist_ok=True)
            
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
                
            extracted_desc = table.get("description", "").strip()
            if extracted_desc:
                description_text = f"{extracted_desc}\n\n[Extracted from {table['source_pdf']} (Page {table.get('page_number', 'N/A')})]"
            else:
                description_text = f"Extracted from {table['source_pdf']} (Page {table.get('page_number', 'N/A')})"
                
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
                        "is_master": is_master,
                        "merged_sources": table.get("merged_sources", [])
                    }
                }
            }
            
            with open(sub_dir / filename, "w", encoding="utf-8") as f:
                json.dump(foundry_table, f, indent=2, ensure_ascii=False)
                
            table_manifest.append({
                "name": table["name"],
                "file": rel_path,
                "source": table["source_pdf"],
                "is_master": is_master,
                "items": len(table["results"])
            })
            
        with open(data_dir / "metadata.json", "w", encoding="utf-8") as f:
            json.dump(table_manifest, f, indent=2, ensure_ascii=False)
            
        styles_content = """/* ==========================================
   TableForge Premium Golden Glassmorphic Dark Theme
   ========================================== */

/* 1. Importer Button inside Foundry Sidebar */
.tableforge-import-btn {
    background: linear-gradient(135deg, #1e1a14, #242019) !important;
    color: #f5c992 !important;
    border: 1px solid #c89c5e !important;
    border-radius: 4px !important;
    padding: 6px 12px !important;
    font-weight: 600 !important;
    text-shadow: 0 1px 2px rgba(0,0,0,0.6) !important;
    box-shadow: 0 2px 4px rgba(0,0,0,0.3) !important;
    cursor: pointer !important;
    transition: all 0.2s ease-in-out !important;
    margin: 6px 0 !important;
    width: 100% !important;
    display: block !important;
    font-family: "Signika", sans-serif !important;
}

.tableforge-import-btn:hover {
    background: linear-gradient(135deg, #2c2720, #3a3229) !important;
    border-color: #f5c992 !important;
    box-shadow: 0 0 10px rgba(200, 156, 94, 0.4) !important;
    transform: translateY(-1px) !important;
}

/* 2. Dialog Window Shell Overrides - Complete Parchment Eradication */
body.game #tableforge-importer-dialog,
body.game #tableforge-importer-dialog.window-app {
    background: #0c0a09 none !important;
    background-color: #0c0a09 !important;
    background-image: none !important;
    color: #ebe9e5 !important;
    border: 1px solid #c89c5e !important;
    box-shadow: 0 10px 40px rgba(0, 0, 0, 0.95) !important;
    border-radius: 6px !important;
    overflow: hidden !important;
}

body.game #tableforge-importer-dialog.window-app .window-content {
    background: #0c0a09 none !important;
    background-color: #0c0a09 !important;
    background-image: none !important;
    color: #ebe9e5 !important;
    display: flex !important;
    flex-direction: column !important;
    overflow: hidden !important;
    height: 100% !important;
    padding: 10px 15px !important;
    box-sizing: border-box !important;
}

/* Title Bar Override */
body.game #tableforge-importer-dialog .window-header {
    background: #151210 !important;
    border-bottom: 1px solid #2e2920 !important;
    color: #ebe9e5 !important;
    font-weight: bold !important;
    font-family: 'Signika', sans-serif !important;
    border-radius: 5px 5px 0 0 !important;
}

body.game #tableforge-importer-dialog .window-header a.close {
    color: #ebe9e5 !important;
    opacity: 0.8 !important;
}

body.game #tableforge-importer-dialog .window-header a.close:hover {
    opacity: 1 !important;
    color: #ffd8a4 !important;
    text-shadow: 0 0 8px #c89c5e !important;
}

/* 3. Layout Container */
.tableforge-container {
    display: flex !important;
    gap: 15px !important;
    flex: 1 !important;
    height: auto !important;
    min-height: 0 !important;
    font-family: 'Signika', sans-serif !important;
    background: #0c0a09 !important;
    padding: 10px 0 !important;
    box-sizing: border-box !important;
}

/* 4. Left Sidebar (Filters & Controls) */
.tableforge-sidebar {
    width: 320px !important;
    display: flex !important;
    flex-direction: column !important;
    gap: 12px !important;
    background: #151210 !important;
    border: 1px solid #2e2920 !important;
    border-radius: 6px !important;
    padding: 12px !important;
    box-shadow: inset 0 0 10px rgba(0, 0, 0, 0.5) !important;
    flex-shrink: 0 !important;
}

/* Sidebar Title styling */
.tableforge-sidebar-title {
    font-size: 11px !important;
    text-transform: uppercase !important;
    letter-spacing: 0.06em !important;
    color: #f5c992 !important;
    font-weight: bold !important;
    display: flex !important;
    justify-content: space-between !important;
    align-items: center !important;
}

.tableforge-reset {
    color: #a59d8e !important;
    cursor: pointer !important;
    font-size: 10px !important;
    text-transform: uppercase !important;
    font-weight: normal !important;
    transition: color 0.12s !important;
}

.tableforge-reset:hover {
    color: #ebe9e5 !important;
}

/* Form Inputs with maximum contrast */
.tableforge-search-box, .tableforge-input, .tableforge-select {
    background: #1e1a14 !important;
    border: 1px solid #2e2920 !important;
    color: #ffffff !important;
    padding: 8px 12px !important;
    border-radius: 4px !important;
    font-size: 13px !important;
    width: 100% !important;
    box-sizing: border-box !important;
    transition: all 0.2s ease !important;
    font-family: 'Signika', sans-serif !important;
}

.tableforge-search-box:focus, .tableforge-input:focus, .tableforge-select:focus {
    border-color: #c89c5e !important;
    box-shadow: 0 0 8px rgba(200, 156, 94, 0.3) !important;
    outline: none !important;
}

.tableforge-search-box::placeholder, .tableforge-input::placeholder {
    color: #8a7c6a !important;
}

/* Secondary Button actions */
.btn-secondary {
    background: #242019 !important;
    color: #ebe9e5 !important;
    border: 1px solid #c89c5e !important;
    border-radius: 4px !important;
    font-size: 12px !important;
    font-weight: bold !important;
    padding: 6px 12px !important;
    cursor: pointer !important;
    transition: all 0.2s ease !important;
    font-family: 'Signika', sans-serif !important;
}

.btn-secondary:hover {
    background: #c89c5e !important;
    color: #0c0a09 !important;
    box-shadow: 0 0 10px rgba(200, 156, 94, 0.4) !important;
}

.btn-secondary:disabled {
    opacity: 0.4 !important;
    cursor: not-allowed !important;
}

/* Pipeline Status Badges */
.status-badge {
    padding: 4px 8px !important;
    border-radius: 4px !important;
    font-size: 11px !important;
    font-weight: bold !important;
    display: inline-flex !important;
    align-items: center !important;
    gap: 5px !important;
}

.status-badge.online {
    background: rgba(123, 179, 108, 0.15) !important;
    color: #7bb36c !important;
    border: 1px solid #7bb36c !important;
}

.status-badge.offline {
    background: rgba(216, 130, 112, 0.15) !important;
    color: #d88270 !important;
    border: 1px solid #d88270 !important;
}

/* Execute Button (Local compiler pipeline) */
.btn-execute {
    background: linear-gradient(135deg, #a5814e, #c89c5e) !important;
    color: #0c0a09 !important;
    border: 1px solid #f5c992 !important;
    border-radius: 4px !important;
    font-weight: bold !important;
    font-size: 13px !important;
    padding: 8px 12px !important;
    cursor: pointer !important;
    transition: all 0.2s ease !important;
    font-family: 'Signika', sans-serif !important;
}

.btn-execute:hover {
    background: linear-gradient(135deg, #c89c5e, #ffd8a4) !important;
    box-shadow: 0 0 10px rgba(200, 156, 94, 0.5) !important;
}

.btn-execute:disabled {
    opacity: 0.4 !important;
    cursor: not-allowed !important;
}

/* 5. Middle Column (Catalog List) */
.tableforge-catalog {
    flex: 1.2 !important;
    display: flex !important;
    flex-direction: column !important;
    gap: 10px !important;
    background: #151210 !important;
    border: 1px solid #2e2920 !important;
    border-radius: 6px !important;
    padding: 12px !important;
    box-shadow: inset 0 0 10px rgba(0, 0, 0, 0.5) !important;
    min-width: 0 !important;
}

.tableforge-list-header {
    display: flex !important;
    justify-content: space-between !important;
    align-items: center !important;
    font-size: 11px !important;
    text-transform: uppercase !important;
    letter-spacing: 0.06em !important;
    color: #f5c992 !important;
    font-weight: bold !important;
}

.tableforge-list {
    flex: 1 !important;
    overflow-y: auto !important;
    display: flex !important;
    flex-direction: column !important;
    gap: 6px !important;
    padding-right: 4px !important;
}

/* Table Card Item in List */
.tableforge-item {
    display: flex !important;
    align-items: center !important;
    gap: 8px !important;
    background: rgba(255, 255, 255, 0.02) !important;
    border: 1px solid #2e2920 !important;
    padding: 8px 10px !important;
    border-radius: 4px !important;
    cursor: pointer !important;
    transition: all 0.2s ease !important;
    border-left: 3px solid transparent !important;
}

.tableforge-item:hover {
    background: rgba(255, 255, 255, 0.05) !important;
    border-color: rgba(200, 156, 94, 0.3) !important;
    transform: translateX(2px) !important;
}

.tableforge-item.active {
    background: rgba(200, 156, 94, 0.15) !important;
    border-color: #c89c5e !important;
    border-left: 3px solid #c89c5e !important;
}

.tableforge-checkbox {
    margin: 0 !important;
    cursor: pointer !important;
    width: 15px !important;
    height: 15px !important;
    accent-color: #c89c5e !important;
}

.tableforge-item .table-name {
    color: #ffffff !important;
    font-size: 13px !important;
    font-weight: bold !important;
}

.tableforge-item-meta {
    font-size: 11px !important;
    color: #a59d8e !important;
    margin-top: 3px !important;
}

/* Status badges for individual table cards */
.tableforge-badge {
    display: inline-block !important;
    padding: 1px 4px !important;
    border-radius: 3px !important;
    font-size: 9px !important;
    font-weight: bold !important;
    text-transform: uppercase !important;
    margin-right: 4px !important;
}

.tableforge-badge.master {
    background: rgba(200, 156, 94, 0.2) !important;
    color: #f5c992 !important;
    border: 1px solid #c89c5e !important;
}

.tableforge-badge.individual {
    background: rgba(165, 157, 142, 0.15) !important;
    color: #cbd5e0 !important;
    border: 1px solid #a59d8e !important;
}

/* 6. Right Column (Preview & Live Log Panel) */
.tableforge-workspace {
    flex: 1.8 !important;
    display: flex !important;
    flex-direction: column !important;
    gap: 15px !important;
    min-width: 0 !important;
}

.tableforge-preview {
    flex: 1.4 !important;
    background: #070605 !important;
    border: 1px solid #2e2920 !important;
    border-radius: 6px !important;
    padding: 15px !important;
    display: flex !important;
    flex-direction: column !important;
    gap: 12px !important;
    overflow-y: auto !important;
    box-shadow: inset 0 0 15px rgba(0, 0, 0, 0.8) !important;
}

.tableforge-preview-header {
    border-bottom: 2px solid #2e2920 !important;
    padding-bottom: 10px !important;
}

.tableforge-preview-header h2 {
    margin: 0 !important;
    color: #f5c992 !important;
    font-size: 18px !important;
    font-weight: bold !important;
}

.tableforge-preview-header code {
    background: #151210 !important;
    border: 1px solid #2e2920 !important;
    color: #f5c992 !important;
    font-family: monospace !important;
    font-size: 12px !important;
    padding: 2px 6px !important;
    border-radius: 3px !important;
}

/* Preview Grid Table style overrides */
.tableforge-grid {
    width: 100% !important;
    border-collapse: collapse !important;
    margin-top: 5px !important;
    font-size: 12px !important;
}

.tableforge-grid th, .tableforge-grid td {
    padding: 6px 10px !important;
    text-align: left !important;
    border-bottom: 1px solid #2e2920 !important;
    color: #ebe9e5 !important;
}

.tableforge-grid th {
    background: #151210 !important;
    color: #f5c992 !important;
    font-weight: bold !important;
    border-bottom: 2px solid #c89c5e !important;
}

.tableforge-grid tr:hover {
    background: rgba(255, 255, 255, 0.02) !important;
}

/* 7. Real-time stdout log Console Window */
.tableforge-console-section {
    flex: 0.8 !important;
    background: #070605 !important;
    border: 1px solid #2e2920 !important;
    border-radius: 6px !important;
    display: flex !important;
    flex-direction: column !important;
    overflow: hidden !important;
    box-shadow: inset 0 0 10px rgba(0, 0, 0, 0.8) !important;
}

.tableforge-progress-bar-container {
    height: 4px !important;
    background: #151210 !important;
    width: 100% !important;
    overflow: hidden !important;
}

.tableforge-progress-bar {
    height: 100% !important;
    background: #c89c5e !important;
    width: 0%;
    transition: width 0.3s ease !important;
}

.tableforge-console {
    flex: 1 !important;
    overflow-y: auto !important;
    padding: 10px !important;
    font-family: monospace !important;
    font-size: 11px !important;
    line-height: 1.4 !important;
    background: transparent !important;
    margin: 0 !important;
}

.tableforge-log-entry {
    margin: 0 0 4px 0 !important;
    word-break: break-all !important;
}

.tableforge-log-entry.success {
    color: #7bb36c !important;
}

.tableforge-log-entry.error {
    color: #d88270 !important;
}

.tableforge-log-entry.info {
    color: #a59d8e !important;
}

/* 8. Import Footer Bar */
.tableforge-import-footer {
    display: flex !important;
    justify-content: flex-end !important;
    align-items: center !important;
    gap: 15px !important;
    margin-top: 15px !important;
    padding-top: 12px !important;
    border-top: 1px solid #2e2920 !important;
    background: #0c0a09 !important;
}

.tableforge-import-footer label {
    font-size: 13px !important;
    color: #f5c992 !important;
    font-weight: bold !important;
}

#import-target {
    background: #1e1a14 !important;
    color: #ffffff !important;
    border: 1px solid #2e2920 !important;
    padding: 6px 12px !important;
    border-radius: 4px !important;
    cursor: pointer !important;
    font-size: 12px !important;
    font-family: 'Signika', sans-serif !important;
    transition: all 0.2s ease !important;
}

#import-target:focus {
    border-color: #c89c5e !important;
    box-shadow: 0 0 8px rgba(200, 156, 94, 0.3) !important;
    outline: none !important;
}

#import-target option {
    background: #151210 !important;
    color: #ffffff !important;
}

.btn-import-execute {
    background: linear-gradient(135deg, #a5814e, #c89c5e) !important;
    color: #0c0a09 !important;
    border: 1px solid #f5c992 !important;
    border-radius: 4px !important;
    font-weight: bold !important;
    font-size: 13px !important;
    padding: 8px 22px !important;
    cursor: pointer !important;
    transition: all 0.2s ease !important;
    box-shadow: 0 2px 4px rgba(0,0,0,0.3) !important;
    text-shadow: 0 1px 1px rgba(255,255,255,0.2) !important;
    font-family: 'Signika', sans-serif !important;
}

.btn-import-execute:hover {
    background: linear-gradient(135deg, #c89c5e, #ffd8a4) !important;
    box-shadow: 0 0 12px rgba(200, 156, 94, 0.5) !important;
    transform: translateY(-1px) !important;
}

/* 9. Scrollbars Override for Gold/Opaque Dark Look */
.tableforge-list::-webkit-scrollbar,
.tableforge-preview::-webkit-scrollbar,
.tableforge-console::-webkit-scrollbar,
.tableforge-sidebar::-webkit-scrollbar {
    width: 6px !important;
}

.tableforge-list::-webkit-scrollbar-track,
.tableforge-preview::-webkit-scrollbar-track,
.tableforge-console::-webkit-scrollbar-track,
.tableforge-sidebar::-webkit-scrollbar-track {
    background: #070605 !important;
    border-radius: 3px !important;
}

.tableforge-list::-webkit-scrollbar-thumb,
.tableforge-preview::-webkit-scrollbar-thumb,
.tableforge-console::-webkit-scrollbar-thumb,
.tableforge-sidebar::-webkit-scrollbar-thumb {
    background: rgba(200, 156, 94, 0.3) !important;
    border-radius: 3px !important;
}

.tableforge-list::-webkit-scrollbar-thumb:hover,
.tableforge-preview::-webkit-scrollbar-thumb:hover,
.tableforge-console::-webkit-scrollbar-thumb:hover,
.tableforge-sidebar::-webkit-scrollbar-thumb:hover {
    background: rgba(200, 156, 94, 0.6) !important;
}"""
        with open(self.output_dir / "styles.css", "w", encoding="utf-8") as f:
            f.write(styles_content)
            
        importer_content = """
class TableForgeImporterDialog extends Application {
    static get defaultOptions() {
        return foundry.utils.mergeObject(super.defaultOptions, {
            id: "tableforge-importer-dialog",
            title: "VTT TableForge: PDF Table Importer",
            template: "modules/jenne-table-forge-importer/scripts/importer.html",
            classes: ["tableforge-dialog"],
            width: 1000,
            height: 750,
            resizable: true
        });
    }

    constructor(options={}) {
        super(options);
        this.tables = [];
        this.selectedTables = new Set();
        this.activePreview = null;
        this.searchQuery = "";
        
        // Local Python Server states
        this.serverUrl = "http://localhost:8055";
        this.isServerConnected = false;
        this.isPipelineRunning = false;
        this.pollTimer = null;
        this.knownLogCount = 0;

        this.loadManifest();
    }

    async loadManifest() {
        try {
            const response = await fetch('/modules/jenne-table-forge-importer/data/metadata.json');
            this.tables = await response.json();
            
            // Re-render window with new data if open
            if (this.rendered) {
                this.render(true);
            }
        } catch (e) {
            ui.notifications.error("Failed to load TableForge tables metadata!");
            console.error(e);
        }
    }

    getData() {
        // Map tables to preserve active checklist selections during re-renders
        const tablesWithSelection = this.tables.map(t => ({
            ...t,
            checked: this.selectedTables.has(t.file)
        }));

        // Extract unique themes and PDFs for selection dropdown lists
        const themes = new Set();
        const pdfs = new Set();
        
        for (const t of this.tables) {
            const parts = t.file.split("/");
            if (parts[0] === "combined") {
                themes.add(parts[1]);
            } else {
                pdfs.add(t.source);
            }
        }
        
        const sortedThemes = Array.from(themes).sort().map(theme => ({
            value: `theme:${theme}`,
            label: theme.charAt(0).toUpperCase() + theme.slice(1).replace("_", " ")
        }));
        
        const sortedPDFs = Array.from(pdfs).sort().map(pdf => ({
            value: `pdf:${pdf}`,
            label: pdf.replace(".pdf", "").split("_").map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(" ")
        }));

        return {
            tables: tablesWithSelection,
            themes: sortedThemes,
            pdfs: sortedPDFs,
            selectedCount: this.selectedTables.size,
            activePreview: this.activePreview,
            searchQuery: this.searchQuery
        };
    }

    // High-performance real-time search & multiple dropdown filters
    applyFilters(html) {
        const query = (html.find(".tableforge-search-box").val() || "").toLowerCase();
        const category = html.find("#filter-category").val() || "all";
        const theme = html.find("#filter-theme").val() || "all";
        const source = html.find("#filter-source").val() || "all";

        html.find(".tableforge-item").each((idx, el) => {
            const tableMeta = this.tables[idx];
            
            // 1. Text Search query
            const nameMatch = tableMeta.name.toLowerCase().includes(query);
            const srcMatch = tableMeta.source.toLowerCase().includes(query);
            const matchesSearch = nameMatch || srcMatch;
            
            // 2. Category Type (combined vs individual)
            const fileParts = tableMeta.file.split("/");
            const isMaster = tableMeta.is_master;
            const itemCategory = isMaster ? "combined" : "individual";
            const matchesCategory = (category === "all" || itemCategory === category);
            
            // 3. Theme filter
            let matchesTheme = false;
            if (theme === "all") {
                matchesTheme = true;
            } else if (itemCategory === "combined") {
                const itemTheme = fileParts[1];
                matchesTheme = (theme === `theme:${itemTheme}`);
            }

            // 4. Source PDF filter
            let matchesSource = false;
            if (source === "all") {
                matchesSource = true;
            } else if (itemCategory === "individual") {
                const pdfName = tableMeta.source;
                matchesSource = (source === `pdf:${pdfName}`);
            }
            
            // Dynamic evaluation
            if (matchesSearch && matchesCategory && matchesTheme && matchesSource) {
                $(el).show();
            } else {
                $(el).hide();
            }
        });
    }

    // Dynamic background connection status polling
    async pollServerStatus(html) {
        if (!this.rendered) return;

        try {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 800); // 800ms limit

            const response = await fetch(`${this.serverUrl}/status`, { signal: controller.signal });
            clearTimeout(timeoutId);
            
            const data = await response.json();
            this.isServerConnected = true;

            // Update GUI connection badge
            const badge = html.find("#pipeline-status");
            badge.removeClass("offline").addClass("online");
            badge.html(`<i class="fa-solid fa-circle-check"></i> Pipeline Connected`);

            // Enable control buttons
            html.find("#btn-pipeline-browse").prop("disabled", false);
            
            // Enforce compiler running logic states
            if (data.running) {
                this.isPipelineRunning = true;
                html.find("#btn-pipeline-execute").hide();
                html.find("#btn-pipeline-cancel").show();
                html.find("#pipeline-path").prop("disabled", true);
                html.find("#btn-pipeline-browse").prop("disabled", true);

                // Update real-time progress bar width
                html.find(".tableforge-progress-bar").css("width", `${data.progress}%`);
            } else {
                // If it just finished compiling, reload catalog checklists automatically
                if (this.isPipelineRunning) {
                    this.isPipelineRunning = false;
                    html.find("#btn-pipeline-execute").show();
                    html.find("#btn-pipeline-cancel").hide();
                    html.find("#pipeline-path").prop("disabled", false);
                    html.find("#btn-pipeline-browse").prop("disabled", false);
                    html.find(".tableforge-progress-bar").css("width", "0%");
                    
                    this.loadManifest();
                }
                
                html.find("#btn-pipeline-execute").prop("disabled", false);
                html.find("#btn-pipeline-execute").show();
                html.find("#btn-pipeline-cancel").hide();
            }

            // Sync scrolling status console outputs
            if (data.logs && data.logs.length !== this.knownLogCount) {
                const consoleLogs = html.find(".tableforge-console");
                consoleLogs.empty();
                data.logs.forEach(log => {
                    consoleLogs.append(`<p class="tableforge-log-entry ${log.type}">${log.message}</p>`);
                });
                consoleLogs.scrollTop(consoleLogs[0].scrollHeight);
                this.knownLogCount = data.logs.length;
            }

        } catch (err) {
            this.isServerConnected = false;
            this.isPipelineRunning = false;
            
            // Update GUI offline badge
            const badge = html.find("#pipeline-status");
            badge.removeClass("online").addClass("offline");
            badge.html(`<i class="fa-solid fa-circle-xmark"></i> Pipeline Offline`);

            // Disable controls
            html.find("#btn-pipeline-execute").prop("disabled", true);
            html.find("#btn-pipeline-execute").show();
            html.find("#btn-pipeline-cancel").hide();
            html.find("#btn-pipeline-browse").prop("disabled", true);
            html.find("#pipeline-path").prop("disabled", false);
        }
    }

    activateListeners(html) {
        super.activateListeners(html);
        
        // Start live python status polling
        this.pollServerStatus(html);
        if (this.pollTimer) clearInterval(this.pollTimer);
        this.pollTimer = setInterval(() => this.pollServerStatus(html), 1000);

        // Apply filters instantly on render
        this.applyFilters(html);
        
        html.find(".tableforge-item").on("click", async (e) => {
            // Avoid trigger conflict on checkboxes
            if (e.target.type === "checkbox" || $(e.target).hasClass("tableforge-checkbox")) {
                return;
            }
            
            const idx = $(e.currentTarget).data("index");
            const tableMeta = this.tables[idx];
            
            try {
                const response = await fetch(`/modules/jenne-table-forge-importer/data/tables/${tableMeta.file}`);
                this.activePreview = await response.json();
                this.render(true);
            } catch (err) {
                ui.notifications.error("Failed to load table preview!");
            }
        });

        html.find(".tableforge-checkbox").on("change", (e) => {
            const file = $(e.currentTarget).data("file");
            if (e.currentTarget.checked) {
                this.selectedTables.add(file);
            } else {
                this.selectedTables.delete(file);
            }
            html.find(".selected-count").text(this.selectedTables.size);
        });

        // 1. Text Search Input listener
        html.find(".tableforge-search-box").on("input", () => {
            this.applyFilters(html);
        });

        // Reset search query button
        html.find(".action-reset-search").on("click", () => {
            html.find(".tableforge-search-box").val("");
            this.applyFilters(html);
        });

        // 2. Category Dropdown Filter listener
        html.find("#filter-category").on("change", (e) => {
            const cat = e.currentTarget.value;
            const themeSelect = html.find("#filter-theme");
            const sourceSelect = html.find("#filter-source");
            
            // Reset children selectors to All
            themeSelect.val("all");
            sourceSelect.val("all");
            
            if (cat === "all") {
                themeSelect.find("option").show();
                sourceSelect.find("option").show();
                html.find("#filter-theme-container").show();
                html.find("#filter-source-container").show();
            } else if (cat === "combined") {
                themeSelect.find("option").show();
                sourceSelect.find("option").hide();
            } else if (cat === "individual") {
                themeSelect.find("option").hide();
                sourceSelect.find("option").show();
            }
            
            this.applyFilters(html);
        });

        // 3. Theme Filter listener
        html.find("#filter-theme").on("change", () => {
            this.applyFilters(html);
        });

        // 4. Source PDF Filter listener
        html.find("#filter-source").on("change", () => {
            this.applyFilters(html);
        });

        // Staging options
        html.find(".select-all-btn").on("click", () => {
            html.find(".tableforge-checkbox:visible").each((i, el) => {
                el.checked = true;
                this.selectedTables.add($(el).data("file"));
            });
            html.find(".selected-count").text(this.selectedTables.size);
        });

        html.find(".deselect-all-btn").on("click", () => {
            html.find(".tableforge-checkbox:visible").each((i, el) => {
                el.checked = false;
                this.selectedTables.delete($(el).data("file"));
            });
            html.find(".selected-count").text(this.selectedTables.size);
        });

        // Python Pipeline Browse Folder button
        html.find("#btn-pipeline-browse").on("click", async () => {
            try {
                const response = await fetch(`${this.serverUrl}/browse`);
                const data = await response.json();
                if (data.path) {
                    html.find("#pipeline-path").val(data.path);
                }
            } catch (err) {
                console.error("Browse Folder Dialog Error:", err);
            }
        });

        // Python Pipeline compiler Execution triggers
        html.find("#btn-pipeline-execute").on("click", async () => {
            const pdfDir = html.find("#pipeline-path").val() || "";
            if (!pdfDir.trim()) {
                return ui.notifications.warn("Please enter or browse for a valid PDF search directory!");
            }

            try {
                const response = await fetch(`${this.serverUrl}/run`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ "pdf_dir": pdfDir })
                });
                
                if (response.ok) {
                    ui.notifications.info("TableForge Extraction Pipeline started successfully in the background. Check logs!");
                    this.pollServerStatus(html);
                } else {
                    const errData = await response.json();
                    ui.notifications.error(`Pipeline launch failed: ${errData.error}`);
                }
            } catch (err) {
                ui.notifications.error("Connection error: Unable to contact local Python Server!");
            }
        });

        // Cancel Pipeline execution trigger
        html.find("#btn-pipeline-cancel").on("click", async () => {
            try {
                await fetch(`${this.serverUrl}/kill`, { method: "POST" });
                ui.notifications.info("Cancellation signal successfully dispatched to background thread.");
            } catch (err) {
                console.error(err);
            }
        });

        // Executing import DB database pipeline operations
        html.find(".btn-import-execute").on("click", async () => {
            if (this.selectedTables.size === 0) {
                ui.notifications.warn("No tables selected for import!");
                return;
            }

            const importTarget = html.find("#import-target").val() || "compendium";
            ui.notifications.info(`Importing ${this.selectedTables.size} tables to ${importTarget === 'compendium' ? 'Compendium' : 'Sidebar'}. Please wait...`);
            let count = 0;
            
            // Helper function to build folder path recursively in Sidebar
            async function getOrCreateSidebarFolder(pathParts) {
                let parentId = null;
                for (const part of pathParts) {
                    let folder = game.folders.find(f => f.name === part && f.type === "RollTable" && f.folder?.id === parentId);
                    if (!folder) {
                        folder = await Folder.create({
                            name: part,
                            type: "RollTable",
                            folder: parentId
                        });
                    }
                    parentId = folder.id;
                }
                return parentId;
            }

            // Helper function to get or create Compendium Folder hierarchy
            async function getOrCreateCompendiumFolder(compendium, pathParts) {
                let parentId = null;
                for (const part of pathParts) {
                    let folder = compendium.folders.find(f => f.name === part && f.folder?.id === parentId);
                    if (!folder) {
                        folder = await Folder.create({
                            name: part,
                            type: "RollTable",
                            folder: parentId
                        }, { pack: compendium.metadata.id });
                    }
                    parentId = folder.id;
                }
                return parentId;
            }

            // Get or create custom compendium if target is compendium
            let compendium = null;
            if (importTarget === "compendium") {
                const packName = "world.tableforge-extracted-tables";
                compendium = game.packs.get(packName);
                if (!compendium) {
                    compendium = await CompendiumCollection.createCompendium({
                        type: "RollTable",
                        label: "TableForge: Extracted Tables",
                        name: "tableforge-extracted-tables"
                    });
                }
            }
            
            for (const file of this.selectedTables) {
                try {
                    const response = await fetch(`/modules/jenne-table-forge-importer/data/tables/${file}`);
                    const tableData = await response.json();
                    
                    const fileParts = file.split("/");
                    const category = fileParts[0]; // "individual" or "combined"
                    
                    let pathParts = [];
                    if (category === "combined") {
                        pathParts.push("TableForge (Combined)");
                        const theme = fileParts[1];
                        const themeCap = theme.charAt(0).toUpperCase() + theme.slice(1).replace("_", " ");
                        pathParts.push(themeCap);
                    } else {
                        pathParts.push("TableForge (Individual)");
                        const pdfName = fileParts[1];
                        const pdfCap = pdfName.split("_").map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");
                        pathParts.push(pdfCap);
                    }

                    if (importTarget === "sidebar") {
                        // 1. Sidebar Directory Import
                        const folderId = await getOrCreateSidebarFolder(pathParts);
                        tableData.folder = folderId;

                        let existing = game.tables.find(t => t.name === tableData.name);
                        if (existing) {
                            await existing.delete();
                        }
                        
                        await RollTable.create(tableData);
                    } else {
                        // 2. Compendium Pack Import
                        const folderId = await getOrCreateCompendiumFolder(compendium, pathParts);
                        tableData.folder = folderId;

                        let existing = compendium.index.find(entry => entry.name === tableData.name);
                        if (existing) {
                            const doc = await compendium.getDocument(existing._id);
                            await doc.delete();
                        }

                        await RollTable.create(tableData, { pack: compendium.metadata.id });
                    }
                    count++;
                } catch (err) {
                    console.error(`Failed to import table ${file}:`, err);
                }
            }
            
            ui.notifications.active = true;
            if (importTarget === "sidebar") {
                ui.notifications.info(`Successfully imported ${count} tables into organized folders inside the Rollable Tables tab!`);
            } else {
                ui.notifications.info(`Successfully imported ${count} tables into the 'TableForge: Extracted Tables' Compendium Pack!`);
            }
            this.close();
        });
    }

    close(options={}) {
        if (this.pollTimer) {
            clearInterval(this.pollTimer);
            this.pollTimer = null;
        }
        return super.close(options);
    }
}

Hooks.on("getSceneControlButtons", (controls) => {
    if (!game.user.isGM) return;

    let jenneSuite = controls.find(c => c.name === "jenne-suite");
    if (!jenneSuite) {
        jenneSuite = {
            name: "jenne-suite",
            title: "Jenne Suite",
            icon: "jenne-gothic-j-icon",
            visible: true,
            tools: []
        };
        controls.push(jenneSuite);
    }

    const importerTool = {
        name: "jenne-table-forge-importer",
        title: "TableForge Importer",
        icon: "fas fa-file-pdf",
        button: true,
        visible: true,
        onClick: () => {
            new TableForgeImporterDialog().render(true);
        }
    };

    if (!jenneSuite.tools.some(t => t.name === "jenne-table-forge-importer")) {
        jenneSuite.tools.push(importerTool);
    }
});
"""
        with open(scripts_dir / "importer.js", "w", encoding="utf-8") as f:
            f.write(importer_content)
            
        html_template = """<div class="tableforge-container">
    <!-- 1. LEFT SIDEBAR (Filters & Python Pipeline Controls) -->
    <aside class="tableforge-sidebar">
        <!-- Text Search -->
        <div class="tableforge-sidebar-section">
            <div class="tableforge-sidebar-title">
                <span>GLOBAL TEXT SEARCH</span>
                <span class="action-reset-search tableforge-reset">(RESET)</span>
            </div>
            <input type="text" class="tableforge-search-box" placeholder="Filter tables by name..." value="{{searchQuery}}">
        </div>
        
        <!-- Type Filter -->
        <div class="tableforge-sidebar-section" style="border-top: 1px solid #2e2920; padding-top: 12px;">
            <div class="tableforge-sidebar-title">TABLE TYPE</div>
            <select id="filter-category" class="tableforge-select">
                <option value="all">All Tables</option>
                <option value="combined">Combined Master Tables</option>
                <option value="individual">Individual PDF Tables</option>
            </select>
        </div>
        
        <!-- Theme Filter -->
        <div class="tableforge-sidebar-section" style="border-top: 1px solid #2e2920; padding-top: 12px;">
            <div class="tableforge-sidebar-title">THEME FILTER</div>
            <select id="filter-theme" class="tableforge-select">
                <option value="all">All Themes</option>
                {{#each themes}}
                <option value="{{this.value}}">{{this.label}}</option>
                {{/each}}
            </select>
        </div>

        <!-- Source Filter -->
        <div class="tableforge-sidebar-section" style="border-top: 1px solid #2e2920; padding-top: 12px;">
            <div class="tableforge-sidebar-title">SOURCE PDF FILTER</div>
            <select id="filter-source" class="tableforge-select">
                <option value="all">All Sources</option>
                {{#each pdfs}}
                <option value="{{this.value}}">{{this.label}}</option>
                {{/each}}
            </select>
        </div>

        <!-- Python Pipeline Execution Console -->
        <div class="tableforge-sidebar-section" style="border-top: 1px solid #2e2920; padding-top: 12px; margin-top: auto; display: flex; flex-direction: column; gap: 10px;">
            <div class="tableforge-sidebar-title" style="color: #f5c992;">PYTHON PIPELINE</div>
            
            <!-- Connection Status Badge -->
            <div style="display:flex; justify-content:flex-start;">
                <span id="pipeline-status" class="status-badge offline">
                    <i class="fa-solid fa-circle-xmark"></i> Pipeline Offline
                </span>
            </div>

            <!-- PDF Folder Path Picker -->
            <div style="display:flex; flex-direction:column; gap:4px;">
                <span style="font-size:11px; color:#a59d8e; font-weight:bold;">PDF INPUT DIRECTORY</span>
                <div style="display:flex; gap:6px;">
                    <input type="text" id="pipeline-path" class="tableforge-input" placeholder="Select or paste absolute path..." style="font-size:11px; flex:1;" value="">
                    <button id="btn-pipeline-browse" class="btn-secondary" style="padding:4px 8px; font-size:11px;" title="Browse local folders via Python Dialog"><i class="fas fa-folder-open"></i> Browse</button>
                </div>
            </div>

            <!-- Execute Compiler Buttons -->
            <button id="btn-pipeline-execute" class="btn-execute" style="width:100%; margin-top:5px;" disabled>
                <i class="fas fa-play"></i> Execute TableForge
            </button>
            <button id="btn-pipeline-cancel" class="btn-secondary" style="width:100%; background:rgba(216,130,112,0.1); border-color:rgba(216,130,112,0.4); color:#d88270; display:none;">
                <i class="fas fa-hand-paper"></i> Cancel Processing
            </button>
        </div>
    </aside>

    <!-- 2. MIDDLE COLUMN (Tables Catalog List) -->
    <section class="tableforge-catalog">
        <div class="tableforge-list-header">
            <span>AVAILABLE TABLES</span>
            <span>Selected: <strong class="selected-count">{{selectedCount}}</strong></span>
        </div>
        
        <div class="tableforge-bulk-actions" style="display:flex; justify-content:space-between; gap:5px; margin-bottom: 5px;">
            <button class="select-all-btn btn-secondary" style="flex:1; padding:4px; font-size:11px;"><i class="fas fa-check-square"></i> Stage All</button>
            <button class="deselect-all-btn btn-secondary" style="flex:1; padding:4px; font-size:11px;"><i class="fas fa-square"></i> Clear All</button>
        </div>

        <div class="tableforge-list">
            {{#each tables}}
            <div class="tableforge-item" data-index="{{@index}}">
                <input type="checkbox" class="tableforge-checkbox" data-file="{{this.file}}" {{#if this.checked}}checked{{/if}} style="margin-right:8px;">
                <div style="flex:1; min-width:0;">
                    <div class="table-name" style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="{{this.name}}">{{this.name}}</div>
                    <div class="tableforge-item-meta" style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
                        {{#if this.is_master}}
                        <span class="tableforge-badge master">Combined</span>
                        {{else}}
                        <span class="tableforge-badge individual">PDF</span>
                        {{/if}}
                        {{this.items}} items | {{this.source}}
                    </div>
                </div>
            </div>
            {{/each}}
        </div>
    </section>

    <!-- 3. RIGHT COLUMN (Live Preview & Status Log Panel) -->
    <section class="tableforge-workspace">
        <div class="tableforge-preview">
            {{#if activePreview}}
                <div class="tableforge-preview-header">
                    <h2>{{activePreview.name}}</h2>
                    <div style="font-size:12px; color:#cbd5e0; margin-top:4px;">
                        <strong>Source:</strong> {{activePreview.description}}<br>
                        <strong>Roll Formula:</strong> <code style="background:#0c0a09; padding:2px 4px; border-radius:3px; color:#ffddf6;">{{activePreview.formula}}</code>
                    </div>
                </div>
                
                <div style="flex:1; overflow-y:auto; min-height:0;">
                    <table class="tableforge-grid">
                        <thead>
                            <tr>
                                <th style="width: 60px;">Range</th>
                                <th>Description</th>
                                <th style="width: 50px;">Weight</th>
                            </tr>
                        </thead>
                        <tbody>
                            {{#each activePreview.results}}
                            <tr>
                                <td><strong>{{this.range.[0]}}{{#if (ne this.range.[0] this.range.[1])}}-{{this.range.[1]}}{{/if}}</strong></td>
                                <td>{{this.text}}</td>
                                <td>{{this.weight}}</td>
                            </tr>
                            {{/each}}
                        </tbody>
                    </table>
                </div>
            {{else}}
                <div style="display:flex; flex-direction:column; align-items:center; justify-content:center; height:100%; text-align:center; padding:20px;">
                    <i class="fas fa-dice" style="font-size:48px; margin-bottom:15px; color:#2e2920;"></i>
                    <h3>No Table Selected</h3>
                    <p>Click on any table in the catalog list to see a live preview of its contents and ranges before importing.</p>
                </div>
            {{/if}}
        </div>

        <!-- Real-time Console Log Window (Matching Beneos) -->
        <div class="tableforge-console-section">
            <div class="tableforge-progress-bar-container">
                <div class="tableforge-progress-bar"></div>
            </div>
            <div class="tableforge-console">
                <p class="tableforge-log-entry info">System ready.</p>
                <p class="tableforge-log-entry info">Apply search & filters in the left sidebar catalog.</p>
                <p class="tableforge-log-entry info">GMs: Run 'python tableforge.py --server' in your directory to enable local PDF extraction directly from this UI!</p>
            </div>
        </div>
    </section>
</div>

<div class="tableforge-import-footer">
    <div style="flex:1; display:flex; align-items:center; gap:15px; font-size:12px; color:#cbd5e0;">
        <div>Selected: <strong style="color:white; margin:0 4px;" class="selected-count">{{selectedCount}}</strong> tables</div>
        <div style="display:flex; align-items:center; gap:5px;">
            <label for="import-target" style="font-weight:bold;">Import Target:</label>
            <select id="import-target">
                <option value="compendium" selected>Compendium Pack (Option B - Clean)</option>
                <option value="sidebar">Sidebar Directory (Option A - Folders)</option>
            </select>
        </div>
    </div>
    <button class="btn-import-execute">
        <i class="fas fa-file-import"></i> Import Selected
    </button>
</div>
"""
        with open(scripts_dir / "importer.html", "w", encoding="utf-8") as f:
            f.write(html_template)
            
# =========================================================================
#   TableForge Background Execution & Local HTTP Server
# =========================================================================
import sys
import threading
import json
import urllib.parse
from http.server import BaseHTTPRequestHandler
import socketserver

# Thread-safe global states
GLOBAL_LOGS = []
GLOBAL_PROGRESS = 0
IS_RUNNING = False
CANCEL_EVENT = threading.Event()

def log_msg(msg, log_type="info"):
    print(f"[{log_type.upper()}] {msg}")
    # Ensure no duplicates in memory list
    if not any(l["message"] == msg for l in GLOBAL_LOGS):
        GLOBAL_LOGS.append({"type": log_type, "message": msg})

class ConsoleLogger:
    def __init__(self, original_stdout):
        self.stdout = original_stdout
    def write(self, message):
        self.stdout.write(message)
        msg_strip = message.strip()
        if msg_strip:
            log_type = "info"
            if msg_strip.startswith("[OK]"):
                log_type = "success"
                msg_strip = msg_strip.replace("[OK]", "").strip()
            elif msg_strip.startswith("[ERROR]"):
                log_type = "error"
                msg_strip = msg_strip.replace("[ERROR]", "").strip()
            elif msg_strip.startswith("[WARN]"):
                log_type = "info"
                
            GLOBAL_LOGS.append({"type": log_type, "message": msg_strip})
    def flush(self):
        self.stdout.flush()

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
                print(f"[ERROR] Failed to open folder picker dialog: {e}")
                
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
                self.wfile.write(json.dumps({"error": "TableForge extraction pipeline is already running!"}).encode('utf-8'))
                return

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "started"}).encode('utf-8'))
            
            # Spawn the pipeline execution inside a background thread
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

def run_compilation_pipeline(pdf_dir, step=None, start_at=None):
    global IS_RUNNING, GLOBAL_PROGRESS, GLOBAL_LOGS, CANCEL_EVENT
    IS_RUNNING = True
    GLOBAL_PROGRESS = 5
    GLOBAL_LOGS.clear()
    CANCEL_EVENT.clear()
    
    print("TableForge local execution pipeline triggered in background thread.")
    print(f"Target folder path: {pdf_dir}")
    
    try:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            env_path = Path(__file__).parent / "tableforge.env"
            if env_path.exists():
                with open(env_path, "r") as f:
                    content = f.read()
                    match = re.search(r"GEMINI_API_KEY\s*=\s*['\"]?([\\w-]+)['\"]?", content)
                    if match:
                        api_key = match.group(1)

        extractor = TableForgeExtractor([Path(pdf_dir)], DEFAULT_OUTPUT_PATH, api_key, skip_processed=True)
        pdf_files = list(Path(pdf_dir).glob("**/*.pdf"))
        if not pdf_files:
            print("[ERROR] No PDF documents found in search folder! Cancelled.")
            IS_RUNNING = False
            return
            
        print(f"[OK] Found {len(pdf_files)} PDF files staged in directory tree.")
        
        all_extracted_tables = []
        skipped_count = 0
        
        for idx, pdf_file in enumerate(pdf_files):
            if CANCEL_EVENT.is_set():
                print("[ERROR] Processing cancelled by GM command.")
                break
                
            print(f"Parsing PDF ({idx+1}/{len(pdf_files)}): {pdf_file.name}")
            extracted = extractor.extract_pdf_tables(pdf_file, step=step, start_at=start_at)
            
            if extracted is None:
                skipped_count += 1
            else:
                all_extracted_tables.extend(extracted)
                
            GLOBAL_PROGRESS = int(((idx + 1) / len(pdf_files)) * 85)

        if CANCEL_EVENT.is_set():
            IS_RUNNING = False
            return

        GLOBAL_PROGRESS = 90
        # Compile module
        if all_extracted_tables or skipped_count > 0:
            print("Writing Foundry VTT module package assets...")
            extractor.generate_vtt_module(all_extracted_tables)
            
        GLOBAL_PROGRESS = 100
        print("[OK] VTT-TableForge processing complete successfully!")
            
    except Exception as e:
        print(f"[ERROR] Pipeline crashed: {e}")
    finally:
        IS_RUNNING = False
def main():
    parser = argparse.ArgumentParser(description="VTT TableForge: Extract and clean RPG tables from PDFs.")
    parser.add_argument("--pdf-dir", default=DEFAULT_PDF_DIR, help="Path to PDF directory")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_PATH, help="Path to output Foundry module")
    parser.add_argument("--api-key", help="Google Gemini API Key (or set GEMINI_API_KEY env)")
    parser.add_argument("--incremental", action="store_true", help="Skip PDFs already successfully parsed in tracker")
    parser.add_argument("--server", action="store_true", help="Start local HTTP micro-server on port 8055 for integration with Foundry VTT importer GUI")
    parser.add_argument("--paid-tier", action="store_true", help="Bypass rate limit delays (use if you have billing enabled in AI Studio)")
    parser.add_argument("--clean", action="store_true", help="Wipe tracker database and clear output tables directory before running")
    parser.add_argument("--step", type=int, choices=[1, 2, 3, 4, 5], help="Run ONLY a specific step (1: OCR, 2: Flatten, 3: Clean, 4: Split, 5: Compile)")
    parser.add_argument("--start-at", type=int, choices=[1, 2, 3, 4, 5], help="Start execution at a specific step and run to the end")
    
    args = parser.parse_args()
    
    if args.clean:
        print("[INFO] Wiping local tracker database and purging output tables directory...")
        tracker_path = Path(__file__).parent / "tableforge_tracker.json"
        if tracker_path.exists():
            try:
                tracker_path.unlink()
                print("  [OK] Deleted tableforge_tracker.json.")
            except Exception as e:
                print(f"  [WARN] Failed to delete tableforge_tracker.json: {e}")
                
        output_tables_path = Path(args.output_dir) / "data" / "tables"
        if output_tables_path.exists():
            import shutil
            try:
                shutil.rmtree(output_tables_path)
                print("  [OK] Cleared output tables directory.")
            except Exception as e:
                print(f"  [WARN] Failed to clear output tables directory: {e}")
    
    if args.server:
        run_http_server(8055)
        return
    
    # Secure API Key loading
    api_key = args.api_key or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        env_path = Path(__file__).parent / "tableforge.env"
        if env_path.exists():
            with open(env_path, "r") as f:
                content = f.read()
                match = re.search(r"GEMINI_API_KEY\s*=\s*['\"]?([\\w-]+)['\"]?", content)
                if match:
                    api_key = match.group(1)
        
        if not api_key:
            print("==========================================================================")
            print("              VTT-TableForge: D&D Random Table PDF Extractor              ")
            print("==========================================================================")
            print("To enable advanced AI OCR and clean table formatting (highly recommended),")
            print("please paste your free Gemini API Key from Google AI Studio:")
            print("(Or press ENTER to run in local layout-heuristics only mode)")
            user_input = input("API Key: ").strip()
            if user_input:
                api_key = user_input
                with open(Path(__file__).parent / "tableforge.env", "w") as f:
                    f.write(f"GEMINI_API_KEY=\"{api_key}\"\n")
                print("[OK] Saved key to tableforge.env file for future runs.")
            else:
                print("Continuing in local heuristics mode...")
                
    skip_processed = args.incremental
    if not skip_processed:
        tracker_path = Path(TRACKER_FILE)
        if tracker_path.exists():
            print("\n--------------------------------------------------------------------------")
            print("Processing Mode Selection:")
            print("Skip PDFs that have already been successfully processed in tableforge_tracker.json?")
            user_select = input("Skip processed files? (Y/n): ").strip().lower()
            if user_select in ["", "y", "yes"]:
                skip_processed = True
                print("[INFO] Incremental active. Skipping already processed files.")
            else:
                print("[INFO] Full scan active. All files will be re-processed.")
                
    if args.pdf_dir != DEFAULT_PDF_DIR:
        pdf_dirs = [args.pdf_dir]
    else:
        env_folders = os.environ.get("PDF_FOLDERS")
        if env_folders:
            pdf_dirs = [d.strip() for d in env_folders.split(";") if d.strip()]
            print(f"[OK] Loaded custom PDF folders from tableforge.env: {pdf_dirs}")
        else:
            pdf_dirs = [args.pdf_dir]
        
    extractor = TableForgeExtractor(pdf_dirs, args.output_dir, api_key, skip_processed, args.paid_tier)
    
    pdf_files = []
    for d in extractor.pdf_dirs:
        if d.exists():
            if d.is_file():
                if d.suffix.lower() == ".pdf":
                    pdf_files.append(d)
                else:
                    print(f"[WARN] File is not a PDF: {d}")
            else:
                pdf_files.extend(list(d.glob("**/*.pdf")))
        else:
            print(f"[WARN] Configured path does not exist: {d}")
            
    if not pdf_files:
        print(f"[ERROR] No PDF files found in queue. Exiting.")
        return
        
    print(f"[OK] Found {len(pdf_files)} PDF documents in queue.")
    
    all_extracted_tables = []
    skipped_count = 0
    
    for pdf_file in pdf_files:
        extracted = extractor.extract_pdf_tables(pdf_file, step=args.step, start_at=args.start_at)
        if extracted is None:
            skipped_count += 1
            tracker_key = str(pdf_file.resolve())
            cached_data = extractor.tracker.get(tracker_key, {}).get("tables_data", [])
            all_extracted_tables.extend(cached_data)
            continue
        all_extracted_tables.extend(extracted)
        
    print(f"\n[OK] Extraction cycle complete.")
    
    if all_extracted_tables or skipped_count > 0:
        print("Writing Foundry VTT module package assets...")
        extractor.generate_vtt_module(all_extracted_tables)
        print("\n[OK] Done! VTT-TableForge completed successfully.")
    else:
        print("[ERROR] No tables were compiled.")
