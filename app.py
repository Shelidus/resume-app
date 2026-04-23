import os
import json
import re
import pdfplumber
import docx
from flask import Flask, request, send_file
from google import genai
from playwright.sync_api import sync_playwright


# ── PDF GENERATION ─────────────────────────────────────

def html_to_pdf(html_path, pdf_path):
    """
    Convert HTML to PDF using Playwright/Chromium.

    KEY FIX: All Playwright margins are set to 0mm.
    - Page 1 header sits flush at the very top (desired).
    - Page 2+ top breathing room is handled purely in CSS
      via margin-top on block elements. Chromium correctly
      applies block-level margins when elements land at the
      top of a continued page, unlike @page margins which
      Chromium applies uniformly to ALL pages (including p1).
    """
    html_path = os.path.abspath(html_path)

    with sync_playwright() as p:
        browser = p.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage"
        ]
    )

        page = browser.new_page()
        page.goto(f"file:///{html_path}", wait_until="networkidle")

        page.pdf(
            path=pdf_path,
            format="A4",
            print_background=True,
            margin={
                "top": "0mm",     # @page CSS handles margins
                "bottom": "0mm",  # @page CSS handles margins
                "left": "0mm",
                "right": "0mm"
            }
        )

        browser.close()


# ── CONFIG ─────────────────────────────────────────────

app = Flask(__name__)

UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "output"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
MODEL = "gemini-3-flash-preview"


# ── TEXT EXTRACTION ────────────────────────────────────

def extract_text(file_path):
    if file_path.endswith(".pdf"):
        with pdfplumber.open(file_path) as pdf:
            return "\n".join([p.extract_text() or "" for p in pdf.pages])

    elif file_path.endswith(".docx"):
        doc = docx.Document(file_path)
        return "\n".join([p.text for p in doc.paragraphs])

    return ""


# ── JSON EXTRACTION ────────────────────────────────────

def extract_json(text):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return match.group(0) if match else text


# ── NORMALIZATION ──────────────────────────────────────

def normalize_data(data):
    """Ensure career and education are always lists of dicts."""

    fixed_career = []
    for item in data.get("career", []):
        if isinstance(item, dict):
            fixed_career.append(item)
        else:
            fixed_career.append({
                "company": str(item),
                "role": "",
                "duration": ""
            })
    data["career"] = fixed_career

    fixed_edu = []
    for item in data.get("education", []):
        if isinstance(item, dict):
            fixed_edu.append(item)
        else:
            fixed_edu.append({
                "degree": str(item),
                "institution": "",
                "location": "",
                "duration": ""
            })
    data["education"] = fixed_edu

    return data


# ── GEMINI PARSER ──────────────────────────────────────

def parse_resume(text):
    prompt = f"""
Convert the resume into STRICT JSON format.

Return ONLY valid JSON with NO markdown fences, NO preamble, NO explanation.

STRUCTURE:
{{
  "name": "",
  "title": "",
  "company_name": "",
  "role": "",
  "duration": "",
  "company_description": "",
  "summary": [],
  "skills": {{
    "category_name": ["skill1", "skill2"]
  }},
  "certifications": [],
  "responsibilities": [],
  "career": [
    {{
      "company": "",
      "role": "",
      "duration": ""
    }}
  ],
  "education": [
    {{
      "degree": "",
      "institution": "",
      "location": "",
      "duration": ""
    }}
  ]
}}

RULES FOR SKILLS:
- "skills" must be dynamic — create categories based on what exists in the resume
- Do NOT force predefined categories
- Group similar skills under meaningful names
- Example categories: "Programming Languages", "Frameworks", "Cloud & DevOps", "Tools", etc.

RULES FOR EDUCATION:
- Extract degree/course name
- Extract institution/college/university
- Extract location if available
- Extract duration or year (e.g., 2021-2025 or 07/2021 - 07/2022)
- ALWAYS return duration even if approximate
- DO NOT merge fields into one string

Resume:
{text}
"""

    response = client.models.generate_content(
        model=MODEL,
        contents=prompt
    )

    raw = response.text or ""
    print("\n🔍 RAW GEMINI RESPONSE:\n", raw)

    cleaned = raw.replace("```json", "").replace("```", "").strip()
    cleaned = extract_json(cleaned)

    try:
        parsed = json.loads(cleaned)
        if not isinstance(parsed, dict):
            raise ValueError("Invalid JSON structure")
        print("\n✅ JSON PARSED SUCCESSFULLY\n")
        return parsed

    except Exception as e:
        print(f"\n❌ JSON PARSE FAILED: {e}\n", cleaned)
        return {}


# ── HTML BUILDERS ──────────────────────────────────────

def build_list(items):
    """Build <li> items for summary and responsibilities."""
    return "\n".join([f"<li>{item}</li>" for item in items])


def build_skills(skills_dict):
    """Build the skills table rows from a dynamic category dict."""
    html = ""
    for category, skills in skills_dict.items():
        tags = "".join([f'<span class="tag">{s}</span>' for s in skills])
        html += f"""
        <div class="skills-row">
          <div class="skills-label">{category}</div>
          <div class="skills-tags">{tags}</div>
        </div>
        """
    return html


def build_certifications(items):
    """Build cert boxes."""
    return "".join([f'<div class="cert-box">{c}</div>' for c in items])


def build_career(items):
    """Build career synopsis rows."""
    html = ""
    for c in items:
        if isinstance(c, dict):
            company_role = f"{c.get('company', '')} — {c.get('role', '')}".strip(" —")
            duration = c.get("duration", "")
            html += f"""
            <div class="career-row">
              <span>{company_role}</span>
              <span>{duration}</span>
            </div>
            """
    return html


def build_education(items):
    """Build education entries."""
    html = ""
    for e in items:
        if isinstance(e, dict):
            degree      = e.get("degree", "").strip()
            institution = e.get("institution", "").strip()
            location    = e.get("location", "").strip()
            duration    = e.get("duration", "").strip()

            line = degree
            if institution:
                line += f" — {institution}" if line else institution
            if location:
                line += f", {location}"
            if duration:
                line += f" ({duration})"

            html += f'<div class="edu-entry">{line}</div>\n'
    return html


# ── TEMPLATE INJECTION ─────────────────────────────────

def generate_resume(data):
    """
    Fill the HTML template with parsed resume data.
    Also injects a .page-break-spacer div at the start of
    .content so page 2+ sections have top breathing room
    (the spacer itself is invisible on page 1).
    """
    with open("template.html", "r", encoding="utf-8") as f:
        html = f.read()

    # Scalar fields
    html = html.replace("{{name}}",                data.get("name", ""))
    html = html.replace("{{title}}",               data.get("title", ""))
    html = html.replace("{{company_name}}",        data.get("company_name", ""))
    html = html.replace("{{role}}",                data.get("role", ""))
    html = html.replace("{{duration}}",            data.get("duration", ""))
    html = html.replace("{{company_description}}", data.get("company_description", ""))

    # Section builders
    html = html.replace("{{summary_points}}",   build_list(data.get("summary", [])))
    html = html.replace("{{skills_section}}",   build_skills(data.get("skills", {})))
    html = html.replace("{{certifications}}",   build_certifications(data.get("certifications", [])))
    html = html.replace("{{responsibilities}}", build_list(data.get("responsibilities", [])))
    html = html.replace("{{career_synopsis}}",  build_career(data.get("career", [])))
    html = html.replace("{{education}}",        build_education(data.get("education", [])))

    # Write output
    output_path = os.path.join(OUTPUT_FOLDER, f"output_{os.getpid()}.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    return output_path


# ── ROUTES ─────────────────────────────────────────────

@app.route("/")
def home():
    return """
    <!DOCTYPE html>
    <html>
    <head>
      <title>Resume Generator</title>
      <style>
        body { font-family: Arial, sans-serif; max-width: 500px; margin: 60px auto; }
        h2   { color: #2c3e50; }
        input[type=file] { margin: 16px 0; display: block; }
        button {
          background: #2c3e50; color: #fff;
          border: none; padding: 10px 24px;
          border-radius: 4px; cursor: pointer; font-size: 14px;
        }
        button:hover { background: #3a5068; }
        .note { font-size: 12px; color: #888; margin-top: 8px; }
      </style>
    </head>
    <body>
      <h2>📄 Resume Generator</h2>
      <form method="POST" action="/upload" enctype="multipart/form-data">
        <label>Upload your resume (.pdf or .docx):</label>
        <input type="file" name="resume" accept=".pdf,.docx" required>
        <button type="submit">Generate PDF Resume</button>
      </form>
      <p class="note">Powered by Gemini + Playwright</p>
    </body>
    </html>
    """


@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("resume")

    if not file:
        return "No file uploaded", 400

    filename  = file.filename or "resume"
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(file_path)

    try:
        print(f"\n📁 File saved: {file_path}")

        print("📄 Extracting text...")
        text = extract_text(file_path)
        print(f"✅ Text extracted: {len(text)} chars")

        print("🤖 Calling Gemini...")
        parsed_data = parse_resume(text)
        print(f"✅ Gemini done: {list(parsed_data.keys()) if parsed_data else 'EMPTY'}")

        parsed_data = normalize_data(parsed_data)

        if not parsed_data:
            return "Failed to parse resume — Gemini returned empty. Check terminal.", 500

        print("🖊️  Generating HTML...")
        output_html = generate_resume(parsed_data)
        print(f"✅ HTML written: {output_html}")

        output_pdf = output_html.replace(".html", ".pdf")

        print("🖨️  Rendering PDF with Playwright...")
        html_to_pdf(output_html, output_pdf)
        print(f"✅ PDF written: {output_pdf}")

        return send_file(output_pdf, as_attachment=True, download_name="resume.pdf")

    except Exception as e:
        import traceback
        error_detail = traceback.format_exc()
        print(f"\n❌ UPLOAD ERROR:\n{error_detail}")
        # Return full traceback in browser so you can see exactly what failed
        return f"<pre>ERROR:\n{error_detail}</pre>", 500


# ── RUN ────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
