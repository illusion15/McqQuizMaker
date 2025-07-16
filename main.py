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

@app.route('/')
def index():
    return render_template('index.html')

def extract_questions_from_pdf(pdf_data):
    doc = fitz.open(stream=pdf_data, filetype="pdf")
    text = ""
    for page in doc:
        text += page.get_text()
    questions = re.split(r"(Q\d{3,4}\..*?)(?=Q\d{3,4}\.|$)", text, flags=re.DOTALL)
    return [questions[i] + questions[i+1] for i in range(1, len(questions)-1, 2)]

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

def process_question_block(block, positive, negative):
    lines = [line.strip() for line in block.split("\n") if line.strip()]
    opts, ans, sol = [], '', ''
    question_lines = []

    capturing_question = True
    capturing_solution = False

    for line in lines:
        if re.match(r"^[A-Da-d][\.\)]\s*", line):
            capturing_question = False
            capturing_solution = False
            option_text = re.sub(r"^[A-Da-d][\.\)]\s*", "", line).strip()
            opts.append(option_text)
        elif line.lower().startswith("correct answer"):
            capturing_solution = False
            match = re.search(r"(\d)", line)
            if match:
                ans = match.group(1)
        elif line.lower().startswith("solution"):
            capturing_question = False
            capturing_solution = True
            sol = line.split(":", 1)[-1].strip()
        elif capturing_question:
            line = re.sub(r"^Q\d{3,4}\.\s*", "", line)
            question_lines.append(line)
        elif capturing_solution:
            sol += " " + line.strip()

    q = " ".join(question_lines)
    return {
        "Question": q,
        "Type": "multiple_choice",
        "Options": opts + ["", "", "", ""][len(opts):],
        "Answer": ans,
        "Solution": sol,
        "Positive Marks": positive,
        "Negative Marks": negative
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
        match = re.match(pattern, block.strip())
        if match:
            num = int(match.group(1))
            base_numbers.append(num)
            if i > 0 and base_numbers[i] != base_numbers[i-1] + 1:
                errors.append(f"Issue at Q{base_numbers[i]} (expected Q{base_numbers[i-1] + 1})")

        opts = len(re.findall(r"^[A-Da-d][\.\)]\s*", block, re.MULTILINE))
        if opts != 4 and match:
            option_issues.append(f"Q{match.group(1)} has {opts} options")

    counts = Counter(base_numbers)
    repeated_questions = [f"Q{num}" for num, count in counts.items() if count > 1]

    uploaded_data["base"] = base_numbers[0] if base_numbers else 1

    filtered_qnums = []
    questions_to_generate = 0
    for block in blocks:
        match = re.match(pattern, block.strip())
        if match:
            q_num = int(match.group(1))
            if uploaded_data["range_start"] <= q_num <= uploaded_data["range_end"]:
                filtered_qnums.append(q_num)
                questions_to_generate += 1

    gen_start = min(filtered_qnums) if filtered_qnums else uploaded_data["range_start"]
    gen_end = max(filtered_qnums) if filtered_qnums else uploaded_data["range_end"]

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
        match = re.match(pattern, block.strip())
        if match:
            q_num = int(match.group(1))
            if range_start <= q_num <= range_end:
                selected_blocks.append(block)

    if not selected_blocks:
        return "No questions found in the selected range.", 400

    document = Document()

    for block in selected_blocks:
        data = process_question_block(block, positive, negative)
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
            row.cells[1].text = value
        document.add_paragraph("")

    if output_format == "pdf":
        import tempfile, os
        with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmp_docx:
            document.save(tmp_docx.name)
            tmp_docx.flush()

            pdf_path = tmp_docx.name.replace(".docx", ".pdf")

            if platform.system() == "Windows":
                from docx2pdf import convert
                convert(tmp_docx.name, pdf_path)
            else:
                return "❌ PDF generation is only supported on Windows with MS Word installed.", 500

            return send_file(pdf_path, as_attachment=True,
                             download_name='Processed_MCQs.pdf',
                             mimetype='application/pdf')

    elif output_format == "zip":
        from zipfile import ZipFile
        import tempfile, os

        with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmp_docx:
            document.save(tmp_docx.name)
            tmp_docx.flush()

            zip_path = tmp_docx.name.replace(".docx", ".zip")
            with ZipFile(zip_path, 'w') as zipf:
                zipf.write(tmp_docx.name, arcname="Processed_MCQs.docx")

            return send_file(zip_path, as_attachment=True,
                             download_name='Processed_MCQs.zip',
                             mimetype='application/zip')

    else:
        output_stream = BytesIO()
        document.save(output_stream)
        output_stream.seek(0)
        return send_file(output_stream, as_attachment=True,
                         download_name='Processed_MCQs.docx',
                         mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')

if __name__ == "__main__":
    app.run(debug=True)
