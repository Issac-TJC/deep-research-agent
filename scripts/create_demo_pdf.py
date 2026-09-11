"""Create an explicitly synthetic, text-based PDF for the reproducible UI demo."""

from pathlib import Path

from reportlab.pdfgen.canvas import Canvas

path = Path("artifacts/demo-paper.pdf")
path.parent.mkdir(parents=True, exist_ok=True)
pdf = Canvas(str(path))
pdf.setTitle("Synthetic retrieval demonstration — not a research paper")
for lines in [
    [
        "Synthetic paper: lexical and semantic retrieval",
        "The method maps queries and documents into a shared vector space.",
        "The experiment compares lexical and semantic ranking.",
        "The authors report rare error identifiers remain a limitation.",
    ],
    [
        "Limitations and hypothetical research directions",
        "The dataset is synthetic; Chinese production performance is unknown.",
        "A future experiment could compare hybrid retrieval and reranking.",
        "No paper code or experiment was executed for this demonstration.",
    ],
]:
    for index, line in enumerate(lines):
        pdf.drawString(45, 750 - 28 * index, line)
    pdf.showPage()
pdf.save()
print(path)
