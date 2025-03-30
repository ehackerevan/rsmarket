from flask import Flask, render_template, request, jsonify
from sqlalchemy import create_engine, text
import pandas as pd
import yfinance as yf
import twstock
import numpy as np
from datetime import datetime, timedelta
import logging
import threading
import os
import json

# 初始化 Flask 應用程式
app = Flask(__name__, template_folder='templates')
logging.basicConfig(level=logging.INFO)

# 進度變數
progress = {'value': 0, 'message': '未開始'}

# 動態獲取資料庫連接
def get_db_connection():
    connection_string = os.environ.get('DATABASE_URL')
    if not connection_string:
        raise ValueError("DATABASE_URL 環境變數未設置，請在 Render 中配置")
    # Render 的 PostgreSQL URL 需要將 "postgres://" 替換為 "postgresql://"
    connection_string = connection_string.replace("postgres://", "postgresql://")
    return create_engine(connection_string)

engine = get_db_connection()

# 資料庫初始化
def init_db():
    try:
        with engine.connect() as conn:
            # 建立 prices 表
            conn.execute(text('''CREATE TABLE IF NOT EXISTS prices (
                                 Date DATE, 
                                 StockCode VARCHAR(10), 
                                 Close REAL, 
                                 PRIMARY KEY (Date, StockCode))'''))
            # 建立 volumes 表
            conn.execute(text('''CREATE TABLE IF NOT EXISTS volumes (
                                 Date DATE, 
                                 StockCode VARCHAR(10), 
                                 Volume REAL, 
                                 PRIMARY KEY (Date, StockCode))'''))
            # 建立 filtered_stocks 表
            conn.execute(text('''CREATE TABLE IF NOT EXISTS filtered_stocks (
                                 Date DATE, 
                                 StockCode VARCHAR(10), 
                                 CompanyName VARCHAR(100), 
                                 Price REAL, 
                                 RS REAL, 
                                 PR REAL, 
                                 Volume REAL, 
                                 FilterType VARCHAR(50), 
                                 ExtraInfo TEXT,
                                 PRIMARY KEY (Date, StockCode, FilterType))'''))
            # 建立 pr_values 表
            conn.execute(text('''CREATE TABLE IF NOT EXISTS pr_values (
                                 Date DATE, 
                                 StockCode VARCHAR(10), 
                                 PR REAL, 
                                 PRIMARY KEY (Date, StockCode))'''))
            conn.commit()
        logging.info("資料庫初始化成功")
    except Exception as e:
        logging.error(f"資料庫初始化失敗: {e}")

# 獲取上市股票代碼
def get_listed_stock_codes():
    try:
        all_stocks = twstock.codes
        listed_stocks = [
            f"{code}.TW" for code, info in all_stocks.items()
            if code.isdigit() and len(code) == 4 and info.type == "股票" and info.market == "上市"
        ]
        listed_stocks.append('^TWII')
        logging.info(f"獲取 {len(listed_stocks)} 支股票代碼")
        return listed_stocks
    except Exception as e:
        logging.error(f"獲取股票代碼失敗: {e}")
        return ['^TWII']

# 下載股票資料並插入資料庫
def download_stock_data(stock_codes, start_date, end_date):
    global progress
    stock_data = {'Close': {}, 'Volume': {}}
    total_stocks = len(stock_codes)
    try:
        with engine.connect() as conn:
            for i, code in enumerate(stock_codes):
                try:
                    df = yf.download(code, start=start_date, end=end_date, progress=False, auto_adjust=False)
                    if df.empty or 'Close' not in df.columns or 'Volume' not in df.columns:
                        logging.warning(f"股票 {code} 無有效資料")
                        continue
                    # 處理收盤價資料
                    closing_prices = df['Close'].reset_index()
                    closing_prices['Date'] = closing_prices['Date'].dt.tz_localize(None)
                    closing_prices.columns = ['Date', code]
                    stock_data['Close'][code] = closing_prices
                    for _, row in closing_prices.iterrows():
                        if pd.notna(row[code]):
                            conn.execute(
                                text("""
                                INSERT INTO prices (Date, StockCode, Close)
                                VALUES (:date, :code, :close)
                                ON CONFLICT (Date, StockCode)
                                DO UPDATE SET Close = EXCLUDED.Close
                                """),
                                {'date': row['Date'].strftime('%Y-%m-%d'), 'code': code, 'close': row[code]}
                            )
                    # 處理成交量資料
                    volumes = df['Volume'].reset_index()
                    volumes['Date'] = volumes['Date'].dt.tz_localize(None)
                    volumes.columns = ['Date', code]
                    stock_data['Volume'][code] = volumes
                    for _, row in volumes.iterrows():
                        if pd.notna(row[code]):
                            conn.execute(
                                text("""
                                INSERT INTO volumes (Date, StockCode, Volume)
                                VALUES (:date, :code, :volume)
                                ON CONFLICT (Date, StockCode)
                                DO UPDATE SET Volume = EXCLUDED.Volume
                                """),
                                {'date': row['Date'].strftime('%Y-%m-%d'), 'code': code, 'volume': row[code]}
                            )
                    progress['value'] = (i + 1) / total_stocks * 50
                    progress['message'] = f"正在下載 {code} ({i + 1}/{total_stocks})"
                except Exception as e:
                    logging.error(f"下載 {code} 失敗: {e}")
                    continue
            conn.commit()
        logging.info("股票資料下載完成")
    except Exception as e:
        logging.error(f"下載股票資料失敗: {e}")
    return stock_data

# 合併資料
def merge_stock_data(stock_data):
    global progress
    try:
        price_df = pd.DataFrame()
        volume_df = pd.DataFrame()
        total_steps = len(stock_data['Close']) + len(stock_data['Volume'])
        step = 0
        for code, df in stock_data['Close'].items():
            if price_df.empty:
                price_df = df
            else:
                price_df = price_df.merge(df, on='Date', how='outer')
            step += 1
            progress['value'] = 50 + (step / total_steps * 25)
            progress['message'] = f"正在合併價格資料 ({step}/{total_steps})"
        for code, df in stock_data['Volume'].items():
            if volume_df.empty:
                volume_df = df
            else:
                volume_df = volume_df.merge(df, on='Date', how='outer')
            step += 1
            progress['value'] = 50 + (step / total_steps * 25)
            progress['message'] = f"正在合併成交量資料 ({step}/{total_steps})"
        price_df.set_index('Date', inplace=True)
        volume_df.set_index('Date', inplace=True)
        if '^TWII' not in price_df.columns:
            logging.error("無法取得大盤指數資料")
            return None, None
        logging.info("資料合併完成")
        return price_df, volume_df
    except Exception as e:
        logging.error(f"合併資料失敗: {e}")
        return None, None

# 計算 IBD RS 值
def calculate_ibd_rs(df):
    global progress
    progress['value'] = 75
    progress['message'] = "正在計算 RS 值"
    days_per_month = 21
    periods = {'3m': 3 * days_per_month, '6m': 6 * days_per_month, '9m': 9 * days_per_month, '12m': 12 * days_per_month}
    weights = {'3m': 0.4, '6m': 0.3, '9m': 0.2, '12m': 0.1}
    rs_data = []
    
    pm_values = pd.Series(index=df.index, dtype=float)
    for date in df.index:
        pm = 0
        for period, period_days in periods.items():
            current_price = df['^TWII'].loc[date]
            past_price = df['^TWII'].shift(period_days).loc[date] if date - pd.Timedelta(days=period_days) in df.index else np.nan
            if pd.notna(current_price) and pd.notna(past_price) and past_price != 0:
                mi = (current_price - past_price) / past_price * 100
                pm += weights[period] * mi
        pm_values.loc[date] = pm if pd.notna(pm) else np.nan
    
    for col in df.columns:
        if col == '^TWII':
            continue
        ps_values = pd.Series(index=df.index, dtype=float)
        for date in df.index:
            ps = 0
            for period, period_days in periods.items():
                current_price = df[col].loc[date]
                past_price = df[col].shift(period_days).loc[date] if date - pd.Timedelta(days=period_days) in df.index else np.nan
                if pd.notna(current_price) and pd.notna(past_price) and past_price != 0:
                    ri = (current_price - past_price) / past_price * 100
                    ps += weights[period] * ri
            ps_values.loc[date] = ps if pd.notna(ps) else np.nan
        rs_series = ps_values - pm_values
        rs_series.name = f"{col}_RS"
        rs_data.append(rs_series)
    
    logging.info("RS 值計算完成")
    return pd.concat(rs_data, axis=1) if rs_data else pd.DataFrame()

# 計算並儲存 PR 值
def calculate_pr(rs_df):
    global progress
    progress['value'] = 80
    progress['message'] = "正在計算並儲存 PR 值"
    pr_data = {}
    try:
        with engine.connect() as conn:
            for col in rs_df.columns:
                if col.endswith('_RS'):
                    stock_code = col.replace('_RS', '')
                    pr_series = rs_df[col].rank(pct=True, na_option='bottom') * 99
                    pr_series.name = f"{stock_code}_PR"
                    pr_data[pr_series.name] = pr_series
                    
                    for date, pr_value in pr_series.items():
                        if pd.notna(pr_value):
                            conn.execute(
                                text("""
                                INSERT INTO pr_values (Date, StockCode, PR)
                                VALUES (:date, :code, :pr)
                                ON CONFLICT (Date, StockCode)
                                DO UPDATE SET PR = EXCLUDED.PR
                                """),
                                {'date': date.strftime('%Y-%m-%d'), 'code': stock_code, 'pr': round(pr_value, 2)}
                            )
            conn.commit()
        logging.info("PR 值計算並儲存完成")
    except Exception as e:
        logging.error(f"儲存 PR 值失敗: {e}")
    return pd.concat(pr_data, axis=1) if pr_data else pd.DataFrame()

# 篩選函數並儲存到資料庫
def save_filtered_to_db(df, filter_type, extra_info_col=None):
    try:
        with engine.connect() as conn:
            for _, row in df.iterrows():
                extra_info = row[extra_info_col] if extra_info_col and extra_info_col in row else None
                conn.execute(
                    text("""
                    INSERT INTO filtered_stocks 
                    (Date, StockCode, CompanyName, Price, RS, PR, Volume, FilterType, ExtraInfo)
                    VALUES (:date, :code, :name, :price, :rs, :pr, :volume, :filter_type, :extra_info)
                    ON CONFLICT (Date, StockCode, FilterType)
                    DO UPDATE SET 
                        CompanyName = EXCLUDED.CompanyName,
                        Price = EXCLUDED.Price,
                        RS = EXCLUDED.RS,
                        PR = EXCLUDED.PR,
                        Volume = EXCLUDED.Volume,
                        ExtraInfo = EXCLUDED.ExtraInfo
                    """),
                    {
                        'date': row['日期'].strftime('%Y-%m-%d'), 
                        'code': row['代號'], 
                        'name': row['公司名稱'],
                        'price': row['股價'], 
                        'rs': row['RS值'], 
                        'pr': row['PR值'], 
                        'volume': row['成交量(張)'],
                        'filter_type': filter_type, 
                        'extra_info': str(extra_info)
                    }
                )
            conn.commit()
        logging.info(f"篩選結果 {filter_type} 已保存至資料庫")
    except Exception as e:
        logging.error(f"保存篩選結果 {filter_type} 失敗: {e}")

def get_company_name(code):
    stock_code = code.replace('.TW', '')
    return twstock.codes[stock_code].name if stock_code in twstock.codes else "未知"

# 條件 1: PR > 90，成交量 > 500張，股價 > 60 天 EMA
def filter_high_pr_stocks(price_df, volume_df, rs_df, pr_df, threshold=90):
    global progress
    progress['value'] = 85
    progress['message'] = "正在篩選 PR > 90 且成交量 > 500 張且股價 > 60EMA 的股票"
    ema60_df = price_df.ewm(span=60, adjust=False).mean()
    recent_date = price_df.index[-1]
    records = []
    for col in pr_df.columns:
        if col.endswith('_PR'):
            pr_value = pr_df.loc[recent_date, col]
            if pd.notna(pr_value) and pr_value > threshold:
                company_code = col.replace('_PR', '')
                pr_series = pr_df[col]
                consecutive_days = 0
                for i in range(len(pr_series) - 1, -1, -1):
                    if pd.notna(pr_series.iloc[i]) and pr_series.iloc[i] > threshold:
                        consecutive_days += 1
                    else:
                        break
                price = price_df.loc[recent_date, company_code]
                volume = volume_df.loc[recent_date, company_code]
                ema60 = ema60_df.loc[recent_date, company_code]
                if (pd.notna(price) and pd.notna(volume) and pd.notna(ema60) and 
                    volume / 1000 > 500 and price > ema60):
                    price = round(price, 2)
                    volume = round(volume / 1000)
                    rs_value = rs_df.loc[recent_date, f"{company_code}_RS"]
                    rs_value = round(rs_value) if pd.notna(rs_value) else np.nan
                    company_name = get_company_name(company_code)
                    records.append({
                        '日期': recent_date,
                        '代號': company_code,
                        '公司名稱': company_name,
                        '股價': price,
                        'RS值': rs_value,
                        'PR值': round(pr_value),
                        '成交量(張)': volume,
                        '連續天數': consecutive_days
                    })
    df = pd.DataFrame(records)
    if not df.empty:
        save_filtered_to_db(df, "HighPR", extra_info_col='連續天數')
    return df

# 條件 2: PR 創 60 或 240 天新高且 PR > 70
def filter_pr_new_high_stocks(price_df, volume_df, rs_df, pr_df, windows=[60, 240], threshold=70):
    global progress
    progress['value'] = 90
    progress['message'] = "正在篩選 PR 創 60/240 天新高且 PR > 70 的股票"
    ema60_df = price_df.ewm(span=60, adjust=False).mean()
    recent_date = price_df.index[-1]
    records = {}
    for col in pr_df.columns:
        if col.endswith('_PR') and pd.notna(pr_df.loc[recent_date, col]) and pr_df.loc[recent_date, col] > threshold:
            company_code = col.replace('_PR', '')
            price = price_df.loc[recent_date, company_code]
            volume = volume_df.loc[recent_date, company_code]
            ema60 = ema60_df.loc[recent_date, company_code]
            if (pd.notna(price) and pd.notna(volume) and pd.notna(ema60) and 
                price > ema60 and volume / 1000 > 500):
                pr_value = pr_df.loc[recent_date, col]
                new_high_days = []
                for window in windows:
                    past_pr = pr_df[col].loc[recent_date - pd.Timedelta(days=window):recent_date]
                    if not past_pr.empty and all(pd.notna(past_pr)) and pr_value == past_pr.max():
                        new_high_days.append(window)
                if new_high_days:
                    price = round(price, 2)
                    volume = round(volume / 1000)
                    rs_value = rs_df.loc[recent_date, f"{company_code}_RS"]
                    rs_value = round(rs_value) if pd.notna(rs_value) else np.nan
                    company_name = get_company_name(company_code)
                    records[company_code] = {
                        '日期': recent_date,
                        '代號': company_code,
                        '公司名稱': company_name,
                        '股價': price,
                        'RS值': rs_value,
                        'PR值': round(pr_value),
                        '成交量(張)': volume,
                        '新高天數': max(new_high_days)
                    }
    df = pd.DataFrame(list(records.values()))
    if not df.empty:
        save_filtered_to_db(df, "PRNewHigh", extra_info_col='新高天數')
    return df

# 條件 3: 創 60 或 240 天新高，PR > 70
def filter_new_high_stocks(price_df, volume_df, rs_df, pr_df, windows=[60, 240], threshold=70):
    global progress
    progress['value'] = 95
    progress['message'] = "正在篩選創 60/240 天新高且 PR > 70 的股票"
    ema60_df = price_df.ewm(span=60, adjust=False).mean()
    recent_date = price_df.index[-1]
    records = {}
    for col in pr_df.columns:
        if col.endswith('_PR') and pd.notna(pr_df.loc[recent_date, col]) and pr_df.loc[recent_date, col] > threshold:
            company_code = col.replace('_PR', '')
            price = price_df.loc[recent_date, company_code]
            volume = volume_df.loc[recent_date, company_code]
            ema60 = ema60_df.loc[recent_date, company_code]
            if (pd.notna(price) and pd.notna(volume) and pd.notna(ema60) and 
                price > ema60 and volume / 1000 > 500):
                new_high_days = []
                for window in windows:
                    past_prices = price_df[company_code].loc[recent_date - pd.Timedelta(days=window):recent_date]
                    if not past_prices.empty and all(pd.notna(past_prices)) and price == past_prices.max():
                        new_high_days.append(window)
                if new_high_days:
                    price = round(price, 2)
                    volume = round(volume / 1000)
                    rs_value = rs_df.loc[recent_date, f"{company_code}_RS"]
                    rs_value = round(rs_value) if pd.notna(rs_value) else np.nan
                    company_name = get_company_name(company_code)
                    records[company_code] = {
                        '日期': recent_date,
                        '代號': company_code,
                        '公司名稱': company_name,
                        '股價': price,
                        'RS值': rs_value,
                        'PR值': round(pr_df.loc[recent_date, col]),
                        '成交量(張)': volume,
                        '新高天數': max(new_high_days)
                    }
    df = pd.DataFrame(list(records.values()))
    if not df.empty:
        save_filtered_to_db(df, "NewHigh", extra_info_col='新高天數')
    return df

# 條件 4: PR < 60，創 60 或 240 天新低
def filter_low_pr_new_low_stocks(price_df, volume_df, rs_df, pr_df, windows=[60, 240], threshold=60):
    global progress
    progress['value'] = 98
    progress['message'] = "正在篩選 PR < 60 且創 60/240 天新低的股票"
    ema240_df = price_df.ewm(span=240, adjust=False).mean()
    recent_date = price_df.index[-1]
    records = {}
    for col in pr_df.columns:
        if col.endswith('_PR') and pd.notna(pr_df.loc[recent_date, col]) and pr_df.loc[recent_date, col] < threshold:
            company_code = col.replace('_PR', '')
            price = price_df.loc[recent_date, company_code]
            volume = volume_df.loc[recent_date, company_code]
            ema240 = ema240_df.loc[recent_date, company_code]
            if (pd.notna(price) and pd.notna(volume) and pd.notna(ema240) and 
                price < ema240 and volume / 1000 > 500):
                new_low_days = []
                for window in windows:
                    past_prices = price_df[company_code].loc[recent_date - pd.Timedelta(days=window):recent_date]
                    if not past_prices.empty and all(pd.notna(past_prices)) and price == past_prices.min():
                        new_low_days.append(window)
                if new_low_days:
                    price = round(price, 2)
                    volume = round(volume / 1000)
                    rs_value = rs_df.loc[recent_date, f"{company_code}_RS"]
                    rs_value = round(rs_value) if pd.notna(rs_value) else np.nan
                    company_name = get_company_name(company_code)
                    records[company_code] = {
                        '日期': recent_date,
                        '代號': company_code,
                        '公司名稱': company_name,
                        '股價': price,
                        'RS值': rs_value,
                        'PR值': round(pr_df.loc[recent_date, col]),
                        '成交量(張)': volume,
                        '新低天數': max(new_low_days)
                    }
    df = pd.DataFrame(list(records.values()))
    if not df.empty:
        save_filtered_to_db(df, "LowPRNewLow", extra_info_col='新低天數')
    return df

# 背景更新任務
def update_data_task():
    global progress
    try:
        progress['value'] = 0
        progress['message'] = '開始更新資料'
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=2*365)
        stock_codes = get_listed_stock_codes()
        
        stock_data = download_stock_data(stock_codes, start_date, end_date)
        price_df, volume_df = merge_stock_data(stock_data)
        if price_df is None or volume_df is None:
            progress['value'] = 0
            progress['message'] = '更新失敗：無大盤資料'
            return
        
        rs_df = calculate_ibd_rs(price_df)
        pr_df = calculate_pr(rs_df)
        
        filter_high_pr_stocks(price_df, volume_df, rs_df, pr_df)
        filter_pr_new_high_stocks(price_df, volume_df, rs_df, pr_df)
        filter_new_high_stocks(price_df, volume_df, rs_df, pr_df)
        filter_low_pr_new_low_stocks(price_df, volume_df, rs_df, pr_df)
        
        progress['value'] = 100
        progress['message'] = '更新完成'
        logging.info("資料更新完成")
    except Exception as e:
        progress['value'] = 0
        progress['message'] = f'更新失敗：{str(e)}'
        logging.error(f"資料更新過程中發生錯誤: {e}")

# 主頁路由
@app.route('/', methods=['GET', 'POST'])
def index():
    init_db()
    try:
        with engine.connect() as conn:
            most_recent_date = conn.execute(text("SELECT MAX(Date) as Date FROM filtered_stocks")).scalar()
            if not most_recent_date:
                logging.warning("資料庫中無數據")
                return render_template('results.html', high_pr=[], pr_new_high=[], new_high=[], low_pr_new_low=[])
            
            high_pr_df = pd.read_sql_query(
                text("SELECT * FROM filtered_stocks WHERE FilterType='HighPR' AND Date=:date"),
                conn, params={'date': most_recent_date}
            )
            pr_new_high_df = pd.read_sql_query(
                text("SELECT * FROM filtered_stocks WHERE FilterType='PRNewHigh' AND Date=:date"),
                conn, params={'date': most_recent_date}
            )
            new_high_df = pd.read_sql_query(
                text("SELECT * FROM filtered_stocks WHERE FilterType='NewHigh' AND Date=:date"),
                conn, params={'date': most_recent_date}
            )
            low_pr_new_low_df = pd.read_sql_query(
                text("SELECT * FROM filtered_stocks WHERE FilterType='LowPRNewLow' AND Date=:date"),
                conn, params={'date': most_recent_date}
            )
        
        return render_template('results.html', 
                              high_pr=high_pr_df.to_dict('records') if not high_pr_df.empty else [],
                              pr_new_high=pr_new_high_df.to_dict('records') if not pr_new_high_df.empty else [],
                              new_high=new_high_df.to_dict('records') if not new_high_df.empty else [],
                              low_pr_new_low=low_pr_new_low_df.to_dict('records') if not low_pr_new_low_df.empty else [])
    except Exception as e:
        logging.error(f"主頁查詢失敗: {e}")
        return render_template('results.html', high_pr=[], pr_new_high=[], new_high=[], low_pr_new_low=[])

# 抓取資料路由
@app.route('/fetch_data', methods=['POST'])
def fetch_data():
    global progress
    if progress['value'] > 0 and progress['value'] < 100:
        return jsonify({'status': 'running'})
    else:
        thread = threading.Thread(target=update_data_task)
        thread.start()
        return jsonify({'status': 'started'})

# 進度查詢路由
@app.route('/progress')
def get_progress():
    global progress
    return jsonify({'progress': progress['value'], 'message': progress['message']})

# 圖表資料路由
@app.route('/get_chart_data/<code>')
def get_chart_data(code):
    try:
        with engine.connect() as conn:
            prices_df = pd.read_sql_query(
                text("SELECT Date, Close FROM prices WHERE StockCode=:code ORDER BY Date DESC LIMIT 60"),
                conn, params={'code': code}
            )
            pr_df = pd.read_sql_query(
                text("SELECT Date, PR FROM pr_values WHERE StockCode=:code ORDER BY Date DESC LIMIT 60"),
                conn, params={'code': code}
            )
            twii_df = pd.read_sql_query(
                text("SELECT Date, Close FROM prices WHERE StockCode='^TWII' ORDER BY Date DESC LIMIT 60"),
                conn
            )
            volume_df = pd.read_sql_query(
                text("SELECT Date, Volume FROM volumes WHERE StockCode=:code ORDER BY Date DESC LIMIT 60"),
                conn, params={'code': code}
            )
            company_name_result = conn.execute(
                text("SELECT CompanyName FROM filtered_stocks WHERE StockCode=:code LIMIT 1"),
                {'code': code}
            ).fetchone()
            company_name = company_name_result[0] if company_name_result else "未知"
        
        dates = prices_df['Date'].tolist()[::-1]
        stock_prices = prices_df['Close'].tolist()[::-1]
        pr_values = pr_df['PR'].tolist()[::-1] if not pr_df.empty else [np.nan] * len(dates)
        twii_values = twii_df['Close'].tolist()[::-1] if not twii_df.empty else [np.nan] * len(dates)
        volumes = [(v / 1000) if pd.notna(v) else np.nan for v in volume_df['Volume'].tolist()[::-1]] if not volume_df.empty else [np.nan] * len(dates)
        
        if len(dates) < 60:
            return jsonify({'error': '歷史數據不足 60 天'})
        
        return jsonify({
            'dates': dates,
            'stock_prices': stock_prices,
            'pr_values': pr_values,
            'twii_values': twii_values,
            'volumes': volumes,
            'company_name': company_name
        })
    except Exception as e:
        logging.error(f"圖表資料查詢失敗: {e}")
        return jsonify({'error': str(e)})

# 自動完成路由
@app.route('/autocomplete')
def autocomplete():
    query = request.args.get('query', '').lower()
    if not query:
        return jsonify([])
    
    try:
        with engine.connect() as conn:
            results = conn.execute(
                text("""
                    SELECT DISTINCT StockCode, CompanyName 
                    FROM filtered_stocks 
                    WHERE StockCode LIKE '%.TW' 
                    AND (StockCode LIKE :query OR CompanyName LIKE :query)
                """),
                {'query': f'%{query}%'}
            ).fetchall()
        
        suggestions = [{"code": row[0], "name": row[1]} for row in results]
        return jsonify(suggestions[:10])
    except Exception as e:
        logging.error(f"自動完成查詢失敗: {e}")
        return jsonify([])

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))  # 預設 5000，如果有 PORT 環境變數則使用
    app.run(host='0.0.0.0', port=port, debug=False)