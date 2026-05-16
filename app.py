import os
import io
from threading import Lock
from flask import Flask, render_template, request, jsonify
import pandas as pd

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024

_lock = Lock()
_df = None

BUDGET_MONTHLY = {
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

CATEGORY_ORDER = list(BUDGET_MONTHLY.keys())


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

    return jsonify({'success': True, 'rows': len(df)})


@app.route('/api/status')
def api_status():
    with _lock:
        has_data = _df is not None
    return jsonify({'has_data': has_data})


@app.route('/api/data')
def api_data():
    with _lock:
        df = _df

    if df is None:
        return jsonify({'status': 'no_data'})

    year_filter = request.args.get('year', type=int)
    pf = build_payment_df(df)

    years = sorted(pf['年'].unique().astype(int).tolist()) if len(pf) > 0 else []

    if year_filter:
        pf = pf[pf['年'] == year_filter]

    if len(pf) == 0:
        return jsonify({
            'status': 'ok', 'years': years, 'categories': [],
            'total': {}, 'date_range': 'データなし', 'max_month': 0,
            'income_total': 0, 'month_income': {},
        })

    max_month = int(pf['月'].max())
    date_range = (
        f"{pf['日付'].min().strftime('%Y/%m/%d')} ～ "
        f"{pf['日付'].max().strftime('%Y/%m/%d')}"
    )

    monthly = pf.groupby(['カテゴリ', '月'])['支出'].sum()
    annual = pf.groupby('カテゴリ')['支出'].sum()

    csv_cats = set(pf['カテゴリ'].unique())
    extra_cats = sorted(csv_cats - set(CATEGORY_ORDER))
    all_cats = CATEGORY_ORDER + extra_cats

    categories = []
    for cat in all_cats:
        mv = {}
        for m in range(1, 13):
            try:
                mv[str(m)] = int(monthly.loc[cat, m])
            except KeyError:
                mv[str(m)] = 0
        ay = int(annual.get(cat, 0))
        bm = BUDGET_MONTHLY.get(cat, 0)
        by = bm * 12
        am = round(ay / max_month) if max_month > 0 else 0
        categories.append({
            'name': cat,
            'budget_month': bm,
            'budget_year': by,
            'actual_month': am,
            'actual_year': ay,
            'diff': by - ay,
            'monthly': mv,
        })

    total_bm = sum(BUDGET_MONTHLY.values())
    total_by = total_bm * 12
    total_ay = int(pf['支出'].sum())
    total_am = round(total_ay / max_month) if max_month > 0 else 0
    month_totals = {
        str(m): int(pf[pf['月'] == m]['支出'].sum()) for m in range(1, 13)
    }

    # Income summary
    inf = build_income_df(df)
    if year_filter:
        inf = inf[inf['年'] == year_filter]
    income_total = int(inf['収入'].sum()) if len(inf) > 0 else 0
    month_income = {
        str(m): int(inf[inf['月'] == m]['収入'].sum()) for m in range(1, 13)
    }

    return jsonify({
        'status': 'ok',
        'years': years,
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
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
