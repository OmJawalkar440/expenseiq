from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from .database import engine, SessionLocal
from . import models

import pandas as pd
from io import StringIO
import io

# Create tables
models.Base.metadata.create_all(bind=engine)

app = FastAPI()
templates = Jinja2Templates(directory="templates")


# ─────────────────────────────────────────────
# Smart Column Detector
# ─────────────────────────────────────────────

AMOUNT_KEYWORDS = [
    "amount", "total", "price", "cost", "spend", "spent",
    "expense", "value", "sum", "payment", "paid", "fee",
    "charge", "debit", "credit", "inr", "usd", "rs", "money"
]

DATE_KEYWORDS = [
    "date", "time", "timestamp", "created", "transaction",
    "posted", "day", "month", "period", "when"
]

CATEGORY_KEYWORDS = [
    "category", "cat", "type", "group", "label", "tag",
    "department", "head", "section", "mode", "purpose",
    "description", "desc", "note", "merchant", "vendor",
    "payee", "name", "title", "narration"
]


def _score(col: str, keywords: list) -> int:
    col = col.lower().strip()
    score = 0
    for kw in keywords:
        if kw == col:
            score += 10          # exact match
        elif kw in col or col in kw:
            score += 5           # partial match
    return score


def detect_column(df: pd.DataFrame, keywords: list, used: set):
    """Return the best-matching column for a keyword group."""
    best_col, best_score = None, 0
    for col in df.columns:
        if col in used:
            continue
        s = _score(col, keywords)
        if s > best_score:
            best_score, best_col = s, col
    return best_col if best_score > 0 else None


def detect_numeric_column(df: pd.DataFrame, used: set):
    """Fallback: pick the first numeric column not already used."""
    for col in df.columns:
        if col in used:
            continue
        if pd.api.types.is_numeric_dtype(df[col]):
            return col
    return None


def smart_map_columns(df: pd.DataFrame) -> dict:
    """Return {'amount': col, 'date': col|None, 'category': col|None}."""
    used = set()

    amount_col = detect_column(df, AMOUNT_KEYWORDS, used)
    if amount_col is None:
        amount_col = detect_numeric_column(df, used)
    if amount_col is None:
        raise ValueError(
            "Could not find an amount/value column. "
            "Please ensure your CSV has a column like 'amount', 'total', 'price', etc."
        )
    used.add(amount_col)

    date_col = detect_column(df, DATE_KEYWORDS, used)
    if date_col:
        used.add(date_col)

    category_col = detect_column(df, CATEGORY_KEYWORDS, used)
    if category_col:
        used.add(category_col)

    return {"amount": amount_col, "date": date_col, "category": category_col}


def clean_amount(series: pd.Series) -> pd.Series:
    """Strip currency symbols, commas, brackets and cast to float."""
    if pd.api.types.is_numeric_dtype(series):
        return series.astype(float)
    cleaned = (
        series.astype(str)
        .str.strip()
        .str.replace(r"[₹$€£,\s]", "", regex=True)
        .str.replace(r"^\((.+)\)$", r"-\1", regex=True)  # (500) → -500
    )
    return pd.to_numeric(cleaned, errors="coerce")


# ─────────────────────────────────────────────
# Home Route (UI)
# ─────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ─────────────────────────────────────────────
# Upload & Analyze
# ─────────────────────────────────────────────

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    try:
        contents = await file.read()

        # ── Read CSV (try UTF-8, fall back to latin-1) ──
        try:
            df = pd.read_csv(StringIO(contents.decode("utf-8")))
        except UnicodeDecodeError:
            df = pd.read_csv(StringIO(contents.decode("latin-1")))

        if df.empty:
            return {"error": "The uploaded CSV file is empty."}

        df.columns = df.columns.str.strip().str.lower()
        print("Detected columns:", list(df.columns))

        # ── Smart Column Mapping ──────────────────
        col_map      = smart_map_columns(df)
        amount_col   = col_map["amount"]
        date_col     = col_map["date"]
        category_col = col_map["category"]
        print("Column mapping →", col_map)

        # ── Amount ───────────────────────────────
        df["_amount"] = clean_amount(df[amount_col])
        df = df.dropna(subset=["_amount"])

        if df.empty:
            return {"error": f"No valid numeric data found in column '{amount_col}'."}

        total_expense     = float(df["_amount"].sum())
        average_expense   = float(df["_amount"].mean())
        highest_expense   = float(df["_amount"].max())
        lowest_expense    = float(df["_amount"].min())
        transaction_count = int(len(df))

        # ── Monthly Trend ─────────────────────────
        monthly_trend  = {}
        date_col_used  = None

        if date_col:
            try:
                df["_date"] = pd.to_datetime(df[date_col], errors="coerce", dayfirst=True)
                df_dated    = df.dropna(subset=["_date"]).copy()

                if not df_dated.empty:
                    df_dated["_month"] = df_dated["_date"].dt.to_period("M").astype(str)
                    monthly_data = df_dated.groupby("_month")["_amount"].sum().sort_index()
                    monthly_trend = {str(k): float(v) for k, v in monthly_data.items()}
                    date_col_used = date_col
            except Exception as e:
                print("Date processing error:", e)

        # ── Category Breakdown ────────────────────
        category_expense    = {}
        top_category        = "N/A"
        top_category_amount = 0.0

        if category_col:
            df["_category"] = (
                df[category_col].astype(str).str.strip().str.title()
                .replace({"Nan": "Uncategorized", "None": "Uncategorized", "": "Uncategorized"})
            )
            cat_totals          = df.groupby("_category")["_amount"].sum()
            category_expense    = {str(k): float(v) for k, v in cat_totals.items()}
            top_category        = str(cat_totals.idxmax())
            top_category_amount = float(cat_totals.max())
        else:
            category_expense    = {"All Expenses": total_expense}
            top_category        = "All Expenses"
            top_category_amount = total_expense

        # ── Auto Insights ─────────────────────────
        insights = []

        if monthly_trend:
            top_month = max(monthly_trend, key=monthly_trend.get)
            insights.append({
                "icon": "📅",
                "title": "Peak Spending Month",
                "text": f"{top_month} was your highest-spend month at ₹{monthly_trend[top_month]:,.0f}."
            })

        if len(category_expense) > 1 and total_expense:
            pct = top_category_amount / total_expense * 100
            insights.append({
                "icon": "🔥",
                "title": "Top Category Dominance",
                "text": f"'{top_category}' accounts for {pct:.1f}% of your total spend."
            })

        if highest_expense > 0 and average_expense > 0:
            ratio = highest_expense / average_expense
            insights.append({
                "icon": "⚡",
                "title": "Spike Alert",
                "text": f"Your largest single transaction is {ratio:.1f}× your average expense."
            })

        if len(category_expense) >= 3:
            insights.append({
                "icon": "🗂️",
                "title": "Spending Spread",
                "text": f"Expenses span {len(category_expense)} categories — great for tracking where money goes."
            })
        elif len(category_expense) == 1:
            insights.append({
                "icon": "💡",
                "title": "Tip",
                "text": "Add a 'category' column to your CSV for richer breakdowns and category charts."
            })

        # ── Save to DB ────────────────────────────
        db = SessionLocal()
        try:
            new_file = models.ExpenseFile(filename=file.filename, total_expense=total_expense)
            db.add(new_file)
            db.commit()
            db.refresh(new_file)
        finally:
            db.close()

        # ── Response ──────────────────────────────
        return {
            "filename":            file.filename,
            "total_expense":       total_expense,
            "average_expense":     average_expense,
            "highest_expense":     highest_expense,
            "lowest_expense":      lowest_expense,
            "transaction_count":   transaction_count,
            "category_expense":    category_expense,
            "top_category":        top_category,
            "top_category_amount": top_category_amount,
            "monthly_trend":       monthly_trend,       # ← fixed key name (was monthly_expense in old frontend)
            "column_info": {
                "amount_col":   amount_col,
                "date_col":     date_col_used,
                "category_col": category_col,
            },
            "insights": insights,
        }

    except ValueError as ve:
        return {"error": str(ve)}
    except Exception as e:
        print("ERROR:", e)
        return {"error": f"Unexpected error: {str(e)}"}


# ─────────────────────────────────────────────
# History API
# ─────────────────────────────────────────────

@app.get("/history")
def get_history():
    db = SessionLocal()
    try:
        records = db.query(models.ExpenseFile).order_by(
            models.ExpenseFile.upload_time.desc()
        ).all()
        return [
            {
                "id":            r.id,
                "filename":      r.filename,
                "total_expense": r.total_expense,
                "upload_time":   r.upload_time,
            }
            for r in records
        ]
    finally:
        db.close()


# ─────────────────────────────────────────────
# Download Report (CSV)
# ─────────────────────────────────────────────

@app.post("/download-report")
async def download_report(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        try:
            df = pd.read_csv(StringIO(contents.decode("utf-8")))
        except UnicodeDecodeError:
            df = pd.read_csv(StringIO(contents.decode("latin-1")))

        df.columns = df.columns.str.strip().str.lower()

        col_map      = smart_map_columns(df)
        amount_col   = col_map["amount"]
        date_col     = col_map["date"]
        category_col = col_map["category"]

        df["_amount"] = clean_amount(df[amount_col])
        df = df.dropna(subset=["_amount"])

        total_expense = df["_amount"].sum()
        avg_expense   = df["_amount"].mean()
        highest       = df["_amount"].max()
        lowest        = df["_amount"].min()
        txn_count     = len(df)

        output = io.StringIO()
        output.write(f"Expense Report — {file.filename}\n\n")

        output.write("=== Summary ===\n")
        pd.DataFrame({
            "Metric": ["Total Expense", "Average Expense", "Highest Expense",
                       "Lowest Expense", "Transaction Count"],
            "Value":  [total_expense, avg_expense, highest, lowest, txn_count]
        }).to_csv(output, index=False)

        if category_col:
            df["_category"] = df[category_col].astype(str).str.strip().str.title()
            cat_df = df.groupby("_category")["_amount"].sum().reset_index()
            cat_df.columns = ["Category", "Total"]
            cat_df = cat_df.sort_values("Total", ascending=False)
            output.write("\n=== Category Breakdown ===\n")
            cat_df.to_csv(output, index=False)

        if date_col:
            try:
                df["_date"] = pd.to_datetime(df[date_col], errors="coerce", dayfirst=True)
                df_d = df.dropna(subset=["_date"]).copy()
                df_d["_month"] = df_d["_date"].dt.to_period("M").astype(str)
                monthly_df = df_d.groupby("_month")["_amount"].sum().reset_index()
                monthly_df.columns = ["Month", "Total"]
                output.write("\n=== Monthly Trend ===\n")
                monthly_df.to_csv(output, index=False)
            except Exception:
                pass

        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=expense_report.csv"}
        )

    except Exception as e:
        return {"error": str(e)}