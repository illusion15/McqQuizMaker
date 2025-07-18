from flask import Flask, render_template, request, send_file, redirect, url_for
import fitz
from docx import Document
from docx.shared import Inches
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from io import BytesIO
import re
from collections import Counter
import platform

app = Flask(__name__, template_folder='.')

uploaded_data = {
    "blocks": [],
    "positive": "2",
    "negative": "0.25",
    "range_start": 1,
    "range_end": 9999,
    "base": None
}

# ✅ Universal option pattern (letter, number, roman numeral)
OPTION_LABEL_RE = re.compile(r"^(\d{1,2}|[A-Da-d]|[ivxlcdmIVXLCDM]{1,4})[\.\)]\s*")

@app.route('/')
def index():
    return render_template('index.html')

def extract_questions_from_pdf(pdf_data):
    doc = fitz.open(stream=pdf_data, filetype="pdf")
    questions = []
    images = {}
    question_pages = {}  # Track which page each question appears on

    # First pass: extract images per page
    for page_number in range(len(doc)):
        page = doc.load_page(page_number)
        page_images = []
        for img in page.get_images(full=True):
            xref = img[0]
            base_image = doc.extract_image(xref)
            img_bytes = base_image["image"]
            page_images.append(img_bytes)
        if page_images:
            images[page_number] = page_images

    # Second pass: extract questions and track page numbers
    full_text = ""
    page_texts = []  # Store text per page for later matching
    
    for page_number in range(len(doc)):
        page = doc.load_page(page_number)
        text = page.get_text()
        full_text += text + "\n"
        page_texts.append(text)
        
        # Find question numbers on this page
        question_matches = re.finditer(r"(Q\d{3,4})\.", text)
        for match in question_matches:
            q_num = match.group(1)
            question_pages[q_num] = page_number

    # Extract all questions from full text
    question_blocks = re.findall(r"(Q\d{3,4}\..*?)(?=Q\d{3,4}\.|$)", full_text, re.DOTALL)
    
    # Associate each question with images from its page
    for block in question_blocks:
        q_num_match = re.search(r"(Q\d{3,4})\.", block)
        if q_num_match:
            q_num = q_num_match.group(1)
            page_num = question_pages.get(q_num, -1)
            question_images = images.get(page_num, [])
            questions.append((block, question_images))
    
    return questions

def set_table_borders(table):
    tbl = table._tbl
    tblPr = tbl.tblPr or OxmlElement('w:tblPr')
    tbl.insert(0, tblPr)
    tblBorders = OxmlElement('w:tblBorders')
    for side in ['top', 'left', 'bottom', 'right', 'insideH', 'insideV']:
        border = OxmlElement(f'w:{side}')
        border.set(qn('w:val'), 'single')
        border.set(qn('w:sz'), '4')
        border.set(qn('w:space'), '0')
        border.set(qn('w:color'), 'auto')
        tblBorders.append(border)
    tblPr.append(tblBorders)

def force_table_indent_and_widths(table):
    tbl = table._tbl
    tblPr = tbl.tblPr or OxmlElement('w:tblPr')
    tblInd = OxmlElement('w:tblInd')
    tblInd.set(qn('w:w'), str(int(Inches(0.2).pt)))
    tblInd.set(qn('w:type'), 'dxa')
    tblPr.append(tblInd)
    tbl.insert(0, tblPr)
    for row in table.rows:
        row.cells[0].width = Inches(1.5)
        row.cells[1].width = Inches(4.85)

def process_question_block(block, positive, negative, images=None):
    block_text = block[0] if isinstance(block, tuple) else block
    images = block[1] if isinstance(block, tuple) and len(block) > 1 else []
    
    lines = [line.strip() for line in block_text.split("\n") if line.strip()]
    opts = []
    raw_options = []
    ans = ''
    sol_lines = []
    question_lines = []

    capturing_question = True
    capturing_option_index = -1
    capturing_solution = False

    for line in lines:
        if OPTION_LABEL_RE.match(line):
            capturing_question = False
            capturing_solution = False
            raw_options.append(line)
            opts.append(OPTION_LABEL_RE.sub("", line).strip())
            capturing_option_index = len(opts) - 1

        elif capturing_option_index != -1 and not line.lower().startswith(("correct answer", "solution")):
            opts[capturing_option_index] += ' ' + line
            raw_options[-1] += ' ' + line

        elif line.lower().startswith("correct answer"):
            match = re.search(r"(\d+)", line)
            if match:
                ans = match.group(1)
            capturing_option_index = -1
            capturing_solution = False

        elif line.lower().startswith("solution"):
            sol_lines.append(line.split(":", 1)[-1].strip())
            capturing_solution = True
            capturing_option_index = -1

        elif capturing_solution:
            sol_lines.append(line.strip())

        elif capturing_question:
            line = re.sub(r"^Q\d{3,4}\.\s*", "", line)
            question_lines.append(line)

    if len(opts) <= 4:
        final_options = opts + ["", "", "", ""][len(opts):]
    else:
        extra_raw_opts = raw_options[:-4]
        core_opts = opts[-4:][::-1]
        question_lines.extend(extra_raw_opts)
        final_options = core_opts

    q = " ".join(question_lines)

    if " A." in q and " B." in q:
        q = re.sub(r'\s([A-Da-d][\.\)])', r'\n\1', q)
    elif "1." in q and "2." in q:
        q = re.sub(r'\s(\d{1,2}\.)', r'\n\1', q)

    solution = " ".join(sol_lines).strip()

    return {
        "Question": q.strip(),
        "Type": "multiple_choice",
        "Options": final_options,
        "Answer": ans,
        "Solution": solution,
        "Positive Marks": positive,
        "Negative Marks": negative,
        "Images": images  # Use only images from this question's page
    }


@app.route('/upload', methods=['POST'])
def upload():
    pdf_file = request.files['pdf_file']
    uploaded_data["positive"] = request.form.get('positive', '2')
    uploaded_data["negative"] = request.form.get('negative', '0.25')

    if request.form.get("generate_all") == "yes":
        uploaded_data["range_start"] = 1
        uploaded_data["range_end"] = 9999
    else:
        try:
            uploaded_data["range_start"] = int(request.form.get('range_start') or 1)
            uploaded_data["range_end"] = int(request.form.get('range_end') or 9999)
        except ValueError:
            return "❌ Invalid range input. Please enter valid numbers or check 'Generate all questions'.", 400

    blocks = extract_questions_from_pdf(pdf_file.read())
    uploaded_data["blocks"] = blocks

    errors = []
    base_numbers = []
    option_issues = []
    repeated_questions = []
    pattern = r"Q(\d{3,4})\."

    for i, block in enumerate(blocks):
        block_text = block[0] if isinstance(block, tuple) else block
        match = re.match(pattern, block_text.strip())
        if match:
            num = int(match.group(1))
            base_numbers.append(num)
            
            # ✅ Sequence error check
            if i > 0 and base_numbers[i] != base_numbers[i-1] + 1:
                errors.append(f"Issue at Q{base_numbers[i]} (expected Q{base_numbers[i-1] + 1})")
        
            # ✅ Count options properly by line, not globally
            lines = block_text.strip().splitlines()
            option_count = sum(1 for line in lines if re.match(r"^[A-Da-d][\.\)]\s*", line.strip()))
            if option_count != 4:
                option_issues.append(f"Q{num} has {option_count} options")

    # ✅ Repeated questions
    counts = Counter(base_numbers)
    repeated_questions = [f"Q{num}" for num, count in counts.items() if count > 1]

    uploaded_data["base"] = base_numbers[0] if base_numbers else 1

    # ✅ Filter for selected question range
    filtered_qnums = []
    questions_to_generate = 0
    for block in blocks:
        block_text = block[0] if isinstance(block, tuple) else block
        match = re.match(pattern, block_text.strip())
        if match:
            q_num = int(match.group(1))
            if uploaded_data["range_start"] <= q_num <= uploaded_data["range_end"]:
                filtered_qnums.append(q_num)
                questions_to_generate += 1

    gen_start = min(filtered_qnums) if filtered_qnums else uploaded_data["range_start"]
    gen_end = max(filtered_qnums) if filtered_qnums else uploaded_data["range_end"]

    # ✅ Ensure lists are not None
    errors = errors or []
    option_issues = option_issues or []
    repeated_questions = repeated_questions or []

    return render_template("diagnose.html",
        total_qs=len(blocks),
        actual_start=base_numbers[0] if base_numbers else 0,
        actual_end=base_numbers[-1] if base_numbers else 0,
        range_start=uploaded_data["range_start"],
        range_end=uploaded_data["range_end"],
        base=uploaded_data["base"],
        option_issues=option_issues,
        errors=errors,
        repeated_questions=repeated_questions,
        questions_to_generate=questions_to_generate,
        gen_start=gen_start,
        gen_end=gen_end
    )

@app.route('/generate', methods=['POST'])
def generate():
    from zipfile import ZipFile
    import io

    confirm = request.form.get("confirm", "no")
    output_format = request.form.get("format", "docx")
    blocks = uploaded_data["blocks"]
    base = uploaded_data["base"]
    positive = uploaded_data["positive"]
    negative = uploaded_data["negative"]
    range_start = uploaded_data["range_start"]
    range_end = uploaded_data["range_end"]

    if confirm == "no":
        return redirect(url_for("index"))

    pattern = r"Q(\d{3,4})\."
    selected_blocks = []

    for block in blocks:
        block_text = block[0] if isinstance(block, tuple) else block
        match = re.match(pattern, block_text.strip())
        if match:
            q_num = int(match.group(1))
            if range_start <= q_num <= range_end:
                selected_blocks.append(block)

    if not selected_blocks:
        return "No questions found in the selected range.", 400

    # Create DOCX in memory
    doc_stream = io.BytesIO()
    document = Document()

    for block in selected_blocks:
        # Pass only relevant images to processing
        data = process_question_block(block, positive, negative)

        # Create a table with question details
        table = document.add_table(rows=10, cols=2)
        table.autofit = False
        force_table_indent_and_widths(table)
        set_table_borders(table)

        labels = ["Question", "Type", "Option", "Option", "Option", "Option",
                  "Answer", "Solution", "Positive Marks", "Negative Marks"]
        values = [data["Question"], data["Type"]] + data["Options"][:4] + [
            data["Answer"], data["Solution"], data["Positive Marks"], data["Negative Marks"]]

        for i, (label, value) in enumerate(zip(labels, values)):
            row = table.rows[i]
            row.cells[0].text = label

            if label == "Question":
                row.cells[1].text = value
                paragraph = row.cells[1].paragraphs[0]

                # Insert images associated with this question
                for img_bytes in data.get("Images", []):
                    run = paragraph.add_run()
                    run.add_picture(BytesIO(img_bytes), width=Inches(4.5))

            else:
                row.cells[1].text = re.sub(r"\s*\n\s*", " ", value).strip()

        document.add_paragraph("")

    # Save document
    document.save(doc_stream)
    doc_stream.seek(0)

    if output_format == "docx":
        return send_file(doc_stream, as_attachment=True,
                         download_name="Processed_MCQs.docx",
                         mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    elif output_format == "zip":
        zip_stream = io.BytesIO()
        with ZipFile(zip_stream, 'w') as zipf:
            zipf.writestr("Processed_MCQs.docx", doc_stream.getvalue())
        zip_stream.seek(0)
        return send_file(zip_stream, as_attachment=True,
                         download_name="quiz_package.zip",
                         mimetype="application/zip")

    return "❌ Only DOCX and ZIP formats are supported on this server.", 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=True)