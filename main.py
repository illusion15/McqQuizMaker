from flask import Flask, render_template, request, send_file, redirect, url_for
import fitz # PyMuPDF
from docx import Document
from docx.shared import Inches
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from io import BytesIO
import re
from collections import Counter
import platform
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Image as ReportLabImage, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
from reportlab.lib.units import inch
import os
from zipfile import ZipFile
import io

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
OPTION_LABEL_RE = re.compile(r"^(\d{1,2}|[A-Za-z]|[ivxlcdmIVXLCDM]{1,4})[\.\)]\s*")

@app.route('/')
def index():
    return render_template('index.html')

def extract_questions_from_pdf(pdf_data):
    doc = fitz.open(stream=pdf_data, filetype="pdf")
    questions = []
    current_question = None
    current_images = []
    question_pattern = re.compile(r"Q\d{1,9}\.")
    page_question_count = {}

    for page_number in range(len(doc)):
        page = doc.load_page(page_number)
        text = page.get_text()
        
        # Extract images for this page
        page_images = []
        for img in page.get_images(full=True):
            xref = img[0]
            base_image = doc.extract_image(xref)
            page_images.append({
                "bytes": base_image["image"],
                "ext": base_image["ext"],
                "width": base_image["width"],
                "height": base_image["height"]
            })
        
        # Count questions on this page
        question_matches = question_pattern.findall(text)
        page_question_count[page_number] = len(question_matches)
        
        # Split text into lines for processing
        lines = text.split('\n')
        for line in lines:
            if question_pattern.match(line.strip()):
                # Save previous question if exists
                if current_question:
                    questions.append((current_question, current_images))
                    current_images = []
                
                # Start new question
                current_question = line
                # Add images found so far to new question
                current_images.extend(page_images)
                page_images = []  # Reset for next images
            elif current_question:
                # Accumulate lines for current question
                current_question += '\n' + line
        
        # After processing page, add any remaining images to current question
        if current_question:
            current_images.extend(page_images)
            page_images = []
    
    # Add last question if exists
    if current_question:
        questions.append((current_question, current_images))
    
    return questions, page_question_count

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
    block_text, images = block
    lines = [line.strip() for line in block_text.split("\n") if line.strip()]
    opts = []
    raw_options = []
    ans = ''
    sol_lines = []
    question_lines = []
    
    # Extract question number
    q_num = ""
    q_num_match = re.match(r"^(Q\d{1,9})\.", block_text.strip())
    if q_num_match:
        q_num = q_num_match.group(1)

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
            line = re.sub(r"^Q\d{1,9}\.\s*", "", line)
            question_lines.append(line)

    if len(opts) <= 4:
        final_options = opts + ["", "", "", ""][len(opts):]
    else:
        extra_raw_opts = raw_options[:-4]
        core_opts = opts[-4:]
        question_lines.extend(extra_raw_opts)
        final_options = core_opts

    q = " ".join(question_lines)

    if " A." in q and " B." in q:
        q = re.sub(r'\s([A-Za-z][\.\)])', r'\n\1', q)
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
        "Images": images,
        "Question Number": q_num
    }

def generate_docx(questions):
    """Generate DOCX document from processed questions"""
    document = Document()
    doc_stream = BytesIO()
    
    for data in questions:
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
                for img in data.get("Images", []):
                    try:
                        run = paragraph.add_run()
                        run.add_break()  # Adding a line break before img
                        run.add_picture(BytesIO(img["bytes"]), width=Inches(2))
                    except Exception as e:
                        print(f"Error adding image to DOCX: {e}")

            else:
                row.cells[1].text = re.sub(r"\s*\n\s*", " ", value).strip()

        document.add_paragraph("")
    
    # Save document
    document.save(doc_stream)
    doc_stream.seek(0)
    return doc_stream

def generate_pdf(questions):
    """Generate PDF document from processed questions"""
    pdf_stream = BytesIO()
    doc = SimpleDocTemplate(pdf_stream, pagesize=letter)
    styles = getSampleStyleSheet()
    elements = []

    for data in questions:
        styles = getSampleStyleSheet()
        normal_style = styles['Normal']

        def format_text_with_linebreaks(text):
            # Add <br/> before list-like patterns
            patterns = [
                r"\s([A-Za-z]\.)",
                r"\s(\d{1,2}\.)",
                r"\s([ivxlcdm]{1,4}\.)",
                r"\s([IVXLCDM]{1,4}\.)"
            ]

            for pattern in patterns:
                text = re.sub(pattern, r"<br/>&nbsp;\1", text)
            
            return text


        # Create table data
        table_data = [
            ["Question", Paragraph(format_text_with_linebreaks(data["Question"]), normal_style)],
            ["Type", data["Type"]],
            ["Option A", Paragraph(data["Options"][0], normal_style)],
            ["Option B", Paragraph(data["Options"][1], normal_style)],
            ["Option C", Paragraph(data["Options"][2], normal_style)],
            ["Option D", Paragraph(data["Options"][3], normal_style)],
            ["Answer", data["Answer"]],
            ["Solution", Paragraph(format_text_with_linebreaks(data["Solution"]), normal_style)],
            ["Positive Marks", data["Positive Marks"]],
            ["Negative Marks", data["Negative Marks"]]
        ]

        # Create PDF table
        table = Table(table_data, colWidths=[1.5 * inch, 5 * inch])
        table.setStyle(TableStyle([
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('FONT', (0, 0), (-1, -1), 'Helvetica', 10),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))

        elements.append(table)

        # Add image after table (simulating line break)
        if data.get("Images"):
            try:
                img = data["Images"][0]  # First image only
                img_stream = BytesIO(img["bytes"])
                aspect_ratio = img["height"] / img["width"]
                img_width = 2 * inch
                img_height = img_width * aspect_ratio

                if img_height > 2 * inch:
                    img_height = 2 * inch
                    img_width = img_height / aspect_ratio

                elements.append(Spacer(1, 0.1 * inch))  # newline effect
                elements.append(ReportLabImage(img_stream, width=img_width, height=img_height))
            except Exception as e:
                print(f"Error rendering image after table: {e}")

        elements.append(Spacer(1, 0.3 * inch))  # space before next question

    # Build PDF
    doc.build(elements)
    pdf_stream.seek(0)
    return pdf_stream

@app.route('/upload', methods=['POST'])
def upload():
    pdf_file = request.files['pdf_file']
    uploaded_data["original_filename"] = pdf_file.filename.rsplit('.', 1)[0]  # remove extension
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

    blocks, page_question_count = extract_questions_from_pdf(pdf_file.read())
    uploaded_data["blocks"] = blocks

    errors = []
    base_numbers = []
    option_issues = []
    repeated_questions = []
    pattern = r"Q(\d{1,9})\."
    multi_page_warnings = []

    # Generate multi-page warnings
    for page_num, count in page_question_count.items():
        if count > 1:
            multi_page_warnings.append(f"Page {page_num+1} has {count} questions. Images on this page are associated with the first question that appears there.")

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
            option_count = sum(1 for line in lines if re.match(r"^[A-Za-z][\.\)]\s*", line.strip()))
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
    multi_page_warnings = multi_page_warnings or []

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
        gen_end=gen_end,
        multi_page_warnings=multi_page_warnings
    )

@app.route('/generate', methods=['POST'])
def generate():
    confirm = request.form.get("confirm", "no")
    output_format = request.form.get("format", "docx")
    blocks = uploaded_data["blocks"]
    positive = uploaded_data["positive"]
    negative = uploaded_data["negative"]
    range_start = uploaded_data["range_start"]
    range_end = uploaded_data["range_end"]

    if confirm == "no":
        return redirect(url_for("index"))

    pattern = r"Q(\d{1,9})\."
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

    # Process all selected questions
    processed_questions = []
    for block in selected_blocks:
        data = process_question_block(block, positive, negative)
        processed_questions.append(data)

    # Get a clean filename from the uploaded PDF name
    base_name = re.sub(r'[\\/*?:"<>|]', "_", uploaded_data.get("original_filename", "Processed_MCQs"))
    docx_filename = f"Bulk_Uploader_of_{base_name}.docx"
    pdf_filename = f"Bulk_Uploader_of_{base_name}.pdf"
    zip_filename = f"Bulk_Uploader_of_{base_name}.zip"

    # Handle different output formats
    if output_format == "docx":
        docx_stream = generate_docx(processed_questions)
        return send_file(
            docx_stream,
            as_attachment=True,
            download_name=docx_filename,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )

    elif output_format == "pdf":
        pdf_stream = generate_pdf(processed_questions)
        return send_file(
            pdf_stream,
            as_attachment=True,
            download_name=pdf_filename,
            mimetype="application/pdf"
        )

    elif output_format == "zip":
        # Create ZIP with both DOCX and PDF
        docx_stream = generate_docx(processed_questions)
        pdf_stream = generate_pdf(processed_questions)
        
        zip_stream = BytesIO()
        with ZipFile(zip_stream, 'w') as zipf:
            zipf.writestr(docx_filename, docx_stream.getvalue())
            zipf.writestr(pdf_filename, pdf_stream.getvalue())
        
        zip_stream.seek(0)
        return send_file(
            zip_stream,
            as_attachment=True,
            download_name=zip_filename,
            mimetype="application/zip"
        )

    return "❌ Only DOCX, PDF, and ZIP formats are supported on this server.", 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=True)