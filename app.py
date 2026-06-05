import warnings
warnings.filterwarnings('ignore')

from flask import Flask, render_template, jsonify
from google.oauth2 import service_account
from googleapiclient.discovery import build
import io
import json
import pandas as pd
import os

app = Flask(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(__file__)
SCOPES   = ['https://www.googleapis.com/auth/drive.readonly']


def _load_credentials():
    """Load service account creds from env variable (production) or local file (dev)."""
    env_json = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
    if env_json:
        # Production (Render): credentials stored as environment variable
        info = json.loads(env_json)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        # Local development: read from service_account.json file
        key_file = os.path.join(BASE_DIR, 'service_account.json')
        return service_account.Credentials.from_service_account_file(key_file, scopes=SCOPES)

FILE_IDS = {
    'orders':      '1Z_iUvjHuVYuQn2QAnrDzvqSeffdbAye5',
    'order_items': '14GVYi99FEPIpQO4ni8_Ra4Aqe5fqeBA1',
    'master':      '1iZ5iuFsAxhS3EIwUm1Du0avLezLjuvzT',
}

# ─── Data helpers ─────────────────────────────────────────────────────────────
_cache: dict = {}


def _drive():
    return build('drive', 'v3', credentials=_load_credentials())


def _fetch_xlsx(drive, file_id: str) -> pd.DataFrame:
    content = drive.files().get_media(fileId=file_id).execute()
    return pd.read_excel(io.BytesIO(content))


def load_data(force: bool = False) -> dict:
    global _cache
    if _cache and not force:
        return _cache

    drive = _drive()
    orders      = _fetch_xlsx(drive, FILE_IDS['orders'])
    order_items = _fetch_xlsx(drive, FILE_IDS['order_items'])
    master      = _fetch_xlsx(drive, FILE_IDS['master'])

    # Enrich customers
    master['Customer_Name'] = master['FirstName'] + ' ' + master['LastName']

    orders = orders.merge(
        master[['Customer_ID', 'Customer_Name', 'Restaurant_Name',
                'Channel', 'RestaurantType', 'NumberOfTables', 'NumberOfStaff']],
        on='Customer_ID', how='left'
    )
    orders['Order_Date'] = pd.to_datetime(orders['Order_Date'])

    _cache = {'orders': orders, 'order_items': order_items, 'master': master}
    return _cache


# ─── Routes ───────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/refresh')
def api_refresh():
    load_data(force=True)
    return jsonify({'status': 'ok'})


@app.route('/api/kpis')
def api_kpis():
    d = load_data()
    orders      = d['orders']
    order_items = d['order_items']

    active_orders = orders[orders['Total_Amount'] > 0]

    return jsonify({
        'total_revenue':      int(orders['Total_Amount'].sum()),
        'total_orders':       len(orders),
        'active_customers':   int(active_orders['Customer_ID'].nunique()),
        'avg_order_value':    int(active_orders['Total_Amount'].mean()) if len(active_orders) else 0,
        'total_discount':     int(order_items['Discount_Amount'].sum()),
        'total_units_sold':   int(order_items['Quantity'].sum()),
    })


@app.route('/api/monthly-trend')
def api_monthly_trend():
    orders = load_data()['orders']
    grp = (
        orders.groupby(orders['Order_Date'].dt.to_period('M'))
        .agg(Revenue=('Total_Amount', 'sum'), Orders=('Order_ID', 'count'))
        .reset_index()
    )
    grp['Order_Date'] = grp['Order_Date'].astype(str)
    return jsonify({
        'labels':   grp['Order_Date'].tolist(),
        'revenues': grp['Revenue'].tolist(),
        'orders':   grp['Orders'].tolist(),
    })


@app.route('/api/daily-trend')
def api_daily_trend():
    orders = load_data()['orders']
    grp = (
        orders.groupby(orders['Order_Date'].dt.date)
        .agg(Revenue=('Total_Amount', 'sum'), Orders=('Order_ID', 'count'))
        .reset_index()
    )
    grp['Order_Date'] = grp['Order_Date'].astype(str)
    return jsonify({
        'labels':   grp['Order_Date'].tolist(),
        'revenues': grp['Revenue'].tolist(),
        'orders':   grp['Orders'].tolist(),
    })


@app.route('/api/top-customers')
def api_top_customers():
    orders = load_data()['orders']
    grp = (
        orders.groupby(['Customer_ID', 'Customer_Name', 'RestaurantType', 'Restaurant_Name'])
        .agg(Revenue=('Total_Amount', 'sum'), Order_Count=('Order_ID', 'count'))
        .reset_index()
        .sort_values('Revenue', ascending=False)
    )
    return jsonify({
        'labels':        grp['Customer_Name'].tolist(),
        'revenues':      grp['Revenue'].tolist(),
        'orders':        grp['Order_Count'].tolist(),
        'types':         grp['RestaurantType'].tolist(),
        'restaurants':   grp['Restaurant_Name'].tolist(),
    })


@app.route('/api/top-staff')
def api_top_staff():
    orders = load_data()['orders']
    grp = (
        orders.groupby('Staff_ID')
        .agg(Revenue=('Total_Amount', 'sum'), Order_Count=('Order_ID', 'count'))
        .reset_index()
        .sort_values('Revenue', ascending=False)
    )
    return jsonify({
        'labels':   grp['Staff_ID'].tolist(),
        'revenues': grp['Revenue'].tolist(),
        'orders':   grp['Order_Count'].tolist(),
    })


@app.route('/api/top-products')
def api_top_products():
    items = load_data()['order_items']
    grp = (
        items.groupby('Product_Name')
        .agg(Revenue=('Item_Total', 'sum'),
             Qty=('Quantity', 'sum'),
             Discount=('Discount_Amount', 'sum'))
        .reset_index()
        .sort_values('Revenue', ascending=False)
    )
    return jsonify({
        'labels':    grp['Product_Name'].tolist(),
        'revenues':  grp['Revenue'].tolist(),
        'quantities': grp['Qty'].tolist(),
        'discounts': grp['Discount'].tolist(),
    })


@app.route('/api/channel-breakdown')
def api_channel():
    orders = load_data()['orders']
    grp = (
        orders.groupby('Channel')
        .agg(Revenue=('Total_Amount', 'sum'),
             Customers=('Customer_ID', 'nunique'))
        .reset_index()
        .sort_values('Revenue', ascending=False)
    )
    return jsonify({
        'labels':    grp['Channel'].tolist(),
        'revenues':  grp['Revenue'].tolist(),
        'customers': grp['Customers'].tolist(),
    })


@app.route('/api/restaurant-types')
def api_restaurant_types():
    orders = load_data()['orders']
    grp = (
        orders.groupby('RestaurantType')
        .agg(Revenue=('Total_Amount', 'sum'), Orders=('Order_ID', 'count'))
        .reset_index()
        .sort_values('Revenue', ascending=False)
    )
    return jsonify({
        'labels':   grp['RestaurantType'].tolist(),
        'revenues': grp['Revenue'].tolist(),
        'orders':   grp['Orders'].tolist(),
    })


@app.route('/api/discount-analysis')
def api_discount_analysis():
    items = load_data()['order_items']
    grp = (
        items.groupby('Product_Name')
        .agg(Gross=('Unit_Price', lambda x: (x * items.loc[x.index, 'Quantity']).sum()),
             Discount=('Discount_Amount', 'sum'),
             Net=('Item_Total', 'sum'))
        .reset_index()
        .sort_values('Net', ascending=False)
    )
    return jsonify({
        'labels':    grp['Product_Name'].tolist(),
        'gross':     grp['Gross'].tolist(),
        'discounts': grp['Discount'].tolist(),
        'net':       grp['Net'].tolist(),
    })


# ─── Run ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print("🚀 Loading data from Google Drive...")
    load_data()
    print("✅ Data loaded. Starting server on http://127.0.0.1:5000")
    app.run(debug=True, port=5000, use_reloader=False)
