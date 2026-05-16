import os
import io
import json
import sqlite3
from threading import Lock
from flask import Flask, render_template, request, jsonify
import pandas as pd

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024

_lock = Lock()
_df = None

DATABASE_PATH = os.environ.get('DATABASE_PATH', 'zaim.db')

DEFAULT_BUDGET = {
    '食費': 100000,
    '日用雑貨': 12000,
    '子ども関連': 60000,
    '交通': 10000,
    '交際費': 5000,
    'エンタメ': 5000,
    '教育・教養': 0,
    '美容・衣服': 5000,
    '医療・保険': 5000,
    '通信': 4000,
    '水道・光熱': 20000,
    '住まい': 120000,
    'クルマ': 5000,
    '税金': 5000,
    '大型出費': 35340,
    '投資': 0,
    'その他': 30000,
    '社会保険': 0,
}

CATEGORY_ORDER = list(DEFAULT_BUDGET.keys())


# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS confirmed (
            year INTEGER PRIMARY KEY,
            confirmed_at TEXT,
            date_range TEXT,
            data TEXT
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS budgets (
            year INTEGER,
            category TEXT,
            amount INTEGER DEFAULT 0,
            reason TEXT DEFAULT '',
            PRIMARY KEY (year, category)
        )
    ''')
    conn.commit()
    conn.close()


# ── CSV helpers ───────────────────────────────────────────────────────────────

def parse_csv(content):
    for enc in ('shift_jis', 'cp932', 'utf-8-sig', 'utf-8'):
        try:
            return pd.read_csv(io.BytesIO(content), encoding=enc)
        except Exception:
            continue
    raise ValueError('CSVを解析できませんでした。文字コードを確認してください。')


def build_payment_df(df):
    pf = df[df['方法'] == 'payment'].copy()
    if '集計の設定' in pf.columns:
        pf = pf[pf['集計の設定'] != '集計しない']
    pf['日付'] = pd.to_datetime(pf['日付'], errors='coerce')
    pf = pf.dropna(subset=['日付'])
    pf['月'] = pf['日付'].dt.month
    pf['年'] = pf['日付'].dt.year
    pf['支出'] = pd.to_numeric(pf['支出'], errors='coerce').fillna(0)
    return pf


def build_income_df(df):
    inf = df[df['方法'] == 'income'].copy()
    inf['日付'] = pd.to_datetime(inf['日付'], errors='coerce')
    inf = inf.dropna(subset=['日付'])
    inf['月'] = inf['日付'].dt.month
    inf['年'] = inf['日付'].dt.year
    inf['収入'] = pd.to_numeric(inf['収入'], errors='coerce').fillna(0)
    return inf


def get_budget_for_year(year):
    """Return dict of {category: {amount, reason}} merged with defaults."""
    conn = get_db()
    rows = conn.execute(
        'SELECT category, amount, reason FROM budgets WHERE year = ?', (year,)
    ).fetchall()
    conn.close()
    db_map = {r['category']: {'amount': r['amount'], 'reason': r['reason']} for r in rows}
    result = {}
    for cat in CATEGORY_ORDER:
        if cat in db_map:
            result[cat] = db_map[cat]
        else:
            result[cat] = {'amount': DEFAULT_BUDGET.get(cat, 0), 'reason': ''}
    return result


def build_api_response(categories_data, income_monthly, date_range, max_month,
                       budget_map, is_confirmed):
    """Build the standard /api/data/<year> response dict."""
    csv_cats = set(categories_data.keys())
    extra_cats = sorted(csv_cats - set(CATEGORY_ORDER))
    all_cats = CATEGORY_ORDER + extra_cats

    categories = []
    total_bm = 0
    total_by = 0
    for cat in all_cats:
        mv = categories_data.get(cat, {})
        # Ensure all 12 months present
        mv_full = {str(m): mv.get(str(m), 0) for m in range(1, 13)}
        ay = sum(mv_full.values())
        if cat in budget_map:
            bm = budget_map[cat]['amount']
            reason = budget_map[cat]['reason']
        else:
            bm = DEFAULT_BUDGET.get(cat, 0)
            reason = ''
        by = bm * 12
        am = round(ay / max_month) if max_month > 0 else 0
        total_bm += bm
        total_by += by
        categories.append({
            'name': cat,
            'budget_month': bm,
            'budget_year': by,
            'actual_month': am,
            'actual_year': ay,
            'diff': by - ay,
            'monthly': mv_full,
            'reason': reason,
        })

    total_ay = sum(c['actual_year'] for c in categories)
    total_am = round(total_ay / max_month) if max_month > 0 else 0
    month_totals = {}
    for m in range(1, 13):
        s = str(m)
        month_totals[s] = sum(c['monthly'][s] for c in categories)

    income_total = sum(income_monthly.get(str(m), 0) for m in range(1, 13))
    month_income = {str(m): income_monthly.get(str(m), 0) for m in range(1, 13)}

    return {
        'status': 'ok',
        'date_range': date_range,
        'max_month': max_month,
        'categories': categories,
        'total': {
            'budget_month': total_bm,
            'budget_year': total_by,
            'actual_month': total_am,
            'actual_year': total_ay,
            'diff': total_by - total_ay,
            'monthly': month_totals,
        },
        'income_total': income_total,
        'month_income': month_income,
        'is_confirmed': is_confirmed,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload():
    global _df
    if 'file' not in request.files or not request.files['file'].filename:
        return jsonify({'error': 'ファイルが選択されていません'}), 400

    content = request.files['file'].read()
    try:
        df = parse_csv(content)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    required = ['日付', 'カテゴリ', '方法', '支出']
    missing = [c for c in required if c not in df.columns]
    if missing:
        return jsonify({'error': f'必須列がありません: {", ".join(missing)}'}), 400

    with _lock:
        _df = df

    pf = build_payment_df(df)
    csv_years = sorted(pf['年'].unique().astype(int).tolist(), reverse=True) if len(pf) > 0 else []

    return jsonify({'success': True, 'rows': len(df), 'csv_years': csv_years})


@app.route('/api/status')
def api_status():
    with _lock:
        df = _df

    has_csv = df is not None
    csv_years = []
    if has_csv:
        pf = build_payment_df(df)
        csv_years = sorted(pf['年'].unique().astype(int).tolist(), reverse=True) if len(pf) > 0 else []

    conn = get_db()
    rows = conn.execute(
        'SELECT year, confirmed_at, date_range FROM confirmed ORDER BY year DESC'
    ).fetchall()
    conn.close()

    confirmed = [{'year': r['year'], 'confirmed_at': r['confirmed_at'],
                  'date_range': r['date_range']} for r in rows]

    return jsonify({
        'has_csv': has_csv,
        'csv_years': csv_years,
        'confirmed': confirmed,
    })


@app.route('/api/data/<int:year>')
def api_data(year):
    # Check if year is confirmed in DB
    conn = get_db()
    row = conn.execute('SELECT data FROM confirmed WHERE year = ?', (year,)).fetchone()
    conn.close()

    budget_map = get_budget_for_year(year)

    if row:
        # Confirmed year — serve from DB
        stored = json.loads(row['data'])
        categories_data = stored.get('categories', {})
        income_monthly = stored.get('income_monthly', {})
        date_range = stored.get('date_range', '')
        max_month = stored.get('max_month', 12)
        # Ensure keys are strings
        categories_data = {k: {str(mk): mv for mk, mv in v.items()}
                           for k, v in categories_data.items()}
        income_monthly = {str(k): v for k, v in income_monthly.items()}
        return jsonify(build_api_response(
            categories_data, income_monthly, date_range, max_month,
            budget_map, is_confirmed=True
        ))

    # Not confirmed — use in-memory CSV
    with _lock:
        df = _df

    if df is None:
        return jsonify({'status': 'no_data'})

    pf = build_payment_df(df)
    pf = pf[pf['年'] == year]

    if len(pf) == 0:
        return jsonify({'status': 'no_data'})

    max_month = int(pf['月'].max())
    date_range = (
        f"{pf['日付'].min().strftime('%Y/%m/%d')} ～ "
        f"{pf['日付'].max().strftime('%Y/%m/%d')}"
    )

    monthly = pf.groupby(['カテゴリ', '月'])['支出'].sum()
    csv_cats = set(pf['カテゴリ'].unique())

    categories_data = {}
    for cat in csv_cats:
        mv = {}
        for m in range(1, 13):
            try:
                mv[str(m)] = int(monthly.loc[cat, m])
            except KeyError:
                mv[str(m)] = 0
        categories_data[cat] = mv

    inf = build_income_df(df)
    inf = inf[inf['年'] == year]
    income_monthly = {str(m): int(inf[inf['月'] == m]['収入'].sum()) for m in range(1, 13)}

    return jsonify(build_api_response(
        categories_data, income_monthly, date_range, max_month,
        budget_map, is_confirmed=False
    ))


@app.route('/api/confirm/<int:year>', methods=['POST'])
def confirm_year(year):
    with _lock:
        df = _df

    if df is None:
        return jsonify({'error': 'CSVが読み込まれていません'}), 400

    pf = build_payment_df(df)
    pf = pf[pf['年'] == year]

    if len(pf) == 0:
        return jsonify({'error': f'{year}年のデータがCSVにありません'}), 400

    max_month = int(pf['月'].max())
    date_range = (
        f"{pf['日付'].min().strftime('%Y/%m/%d')} ～ "
        f"{pf['日付'].max().strftime('%Y/%m/%d')}"
    )

    monthly = pf.groupby(['カテゴリ', '月'])['支出'].sum()
    csv_cats = set(pf['カテゴリ'].unique())

    categories_data = {}
    for cat in csv_cats:
        mv = {}
        for m in range(1, 13):
            try:
                mv[str(m)] = int(monthly.loc[cat, m])
            except KeyError:
                mv[str(m)] = 0
        categories_data[cat] = mv

    inf = build_income_df(df)
    inf = inf[inf['年'] == year]
    income_monthly = {str(m): int(inf[inf['月'] == m]['収入'].sum()) for m in range(1, 13)}

    from datetime import datetime, timezone
    confirmed_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    stored = {
        'categories': categories_data,
        'income_monthly': income_monthly,
        'date_range': date_range,
        'max_month': max_month,
    }

    conn = get_db()
    conn.execute(
        'INSERT OR REPLACE INTO confirmed (year, confirmed_at, date_range, data) VALUES (?, ?, ?, ?)',
        (year, confirmed_at, date_range, json.dumps(stored, ensure_ascii=False))
    )
    conn.commit()
    conn.close()

    return jsonify({'success': True, 'year': year, 'confirmed_at': confirmed_at})


@app.route('/api/confirm/<int:year>', methods=['DELETE'])
def unconfirm_year(year):
    conn = get_db()
    conn.execute('DELETE FROM confirmed WHERE year = ?', (year,))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'year': year})


@app.route('/api/budget/<int:year>')
def get_budget(year):
    budget_map = get_budget_for_year(year)
    return jsonify({
        'cat_order': CATEGORY_ORDER,
        'budget': budget_map,
    })


@app.route('/api/budget/<int:year>', methods=['PUT'])
def save_budget(year):
    data = request.get_json()
    if not data or 'budget' not in data:
        return jsonify({'error': '不正なリクエスト'}), 400

    conn = get_db()
    for cat, vals in data['budget'].items():
        amount = int(vals.get('amount', 0))
        reason = str(vals.get('reason', ''))
        conn.execute(
            'INSERT OR REPLACE INTO budgets (year, category, amount, reason) VALUES (?, ?, ?, ?)',
            (year, cat, amount, reason)
        )
    conn.commit()
    conn.close()

    return jsonify({'success': True})


# ── Init ──────────────────────────────────────────────────────────────────────

init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
