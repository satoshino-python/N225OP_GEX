import os
import datetime
import requests
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from io import StringIO, BytesIO
import re

def get_target_date():
    # === 【テスト用】任意の日付を指定する場合 ===
    # テストしたい日付（YYYYMMDD形式）をここに文字列で記述します
    return "20260619"

    # === 【本番用】自動で当日のJST日付を取得する場合（現在はコメントアウト） ===
    # jst_now = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    # return jst_now.strftime("%Y%m%d")

def download_jpx_data(date_str):
    print(f"--- JPXデータ取得開始: {date_str} ---")
    
    # ユーザー指定のURLフォーマット
    oi_url = f"https://www.jpx.co.jp/markets/derivatives/trading-volume/tvdivq00000014nn-att/{date_str}open_interest.xlsx"
    tp_url = f"https://www.jpx.co.jp/automation/markets/derivatives/option-price/files/ose{date_str}tp.csv"
    
    headers = {"User-Agent": "Mozilla/5.0"}
    df_oi, df_tp = None, None
    
    # 1. 建玉残高 (Excel) のダウンロード
    res_oi = requests.get(oi_url, headers=headers)
    if res_oi.status_code == 200:
        try:
            # 【重要】明示的に sheet_name="別紙1" を指定して読み込みます
            df_oi = pd.read_excel(BytesIO(res_oi.content), sheet_name="別紙1")
            print("建玉残高（別紙1シート）の取得に成功しました。")
        except Exception as e:
            print(f"エラー: Excelのシート「別紙1」の読み込みに失敗しました: {e}")
            df_oi = None
    else:
        print(f"建玉残高の取得に失敗 (Status: {res_oi.status_code})")
        
    # 2. 理論価格 (CSV) のダウンロード
    res_tp = requests.get(tp_url, headers=headers)
    if res_tp.status_code == 200:
        try:
            content = res_tp.content.decode('utf-8')
        except UnicodeDecodeError:
            content = res_tp.content.decode('shift_jis')
        df_tp = pd.read_csv(StringIO(content))
        print("理論価格の取得に成功しました。")
    else:
        print(f"理論価格の取得に失敗 (Status: {res_tp.status_code})")
        
    return df_oi, df_tp

def process_data(df_oi, df_tp):
    print("--- データの加工・成形（別紙1シート対応版） ---")
    
    if df_oi is None or df_oi.empty:
        print("警告: 建玉残高データ(df_oi)がないため、加工をスキップします。")
        return pd.DataFrame()

    # 別紙1シートのデータフレームをコピー
    df_clean = df_oi.copy()

    # A~E列 (0~4列目): プットデータ / G~K列 (6~10列目): コールデータ
    put_cols = [0, 1, 2, 3, 4]
    call_cols = [6, 7, 8, 9, 10]
    temp_columns = ["限月取引", "取引高", "当日建玉残高", "前日比", "前日建玉残高"]
    
    # プットデータの切り出し
    df_put = df_clean.iloc[:, put_cols].copy()
    df_put.columns = temp_columns
    
    # コールデータの切り出し
    df_call = df_clean.iloc[:, call_cols].copy()
    df_call.columns = temp_columns
    
    # 縦に結合 (プットとコールを同一テーブル化)
    df_combined = pd.concat([df_put, df_call], ignore_index=True)
    
    # 文字列型に強制変換し、前後の空白を削除
    df_combined["限月取引"] = df_combined["限月取引"].astype(str).str.strip()
    
    # "NIKKEI" が含まれるデータ行のみを抽出（大文字小文字・スペースのブレを許容）
    df_combined = df_combined[df_combined["限月取引"].str.contains("NIKKEI", na=False, case=False)]
    
    # 「限月合計」などの集計行を除外
    df_combined = df_combined[~df_combined["限月取引"].str.contains("合計", na=False)]
    
    if df_combined.empty:
        print("⚠️ フィルタリング後のデータが空になりました。別紙1シートが正しく読み込めていない可能性があります。")
        return pd.DataFrame()

    # スペースの数に影響されない正規表現で「種別」「限月」「権利行使価格」を抽出
    pattern = r"NIKKEI\s*225\s*([PC])(\d{4})-(\d+)"
    extracted = df_combined["限月取引"].str.extract(pattern, flags=re.IGNORECASE)
    
    df_combined["種別シグナル"] = extracted[0].str.upper()
    df_combined["限月"] = extracted[1]
    df_combined["権利行使価格"] = extracted[2]
    
    # P/Cを put/call に変換
    df_combined["プットコール種別"] = df_combined["種別シグナル"].map({"P": "put", "C": "call"})
    
    # 分解できなかった行を排除
    df_combined = df_combined.dropna(subset=["プットコール種別", "限月", "権利行使価格"])
    
    # 取得日（テストなら20260619）を設定
    current_date = get_target_date() 
    df_combined["取得日"] = current_date
    
    # 指定された順番（左から8列）に並び替え
    final_cols = [
        "取得日", 
        "プットコール種別", 
        "限月", 
        "権利行使価格", 
        "取引高", 
        "当日建玉残高", 
        "前日比", 
        "前日建玉残高"
    ]
    df_final = df_combined[final_cols].copy()
    
    # 数値列のカンマやハイフンを数字の0に安全変換
    num_cols = ["権利行使価格", "取引高", "当日建玉残高", "前日比", "前日建玉残高"]
    for col in num_cols:
        df_final[col] = df_final[col].astype(str).str.replace(r'[\s,]', '', regex=True)
        df_final[col] = pd.to_numeric(df_final[col].replace('-', '0').replace('nan', '0'), errors='coerce').fillna(0).astype(int)

    # インデックスを振り直す
    df_final = df_final.reset_index(drop=True)

    print(f"加工完了: 別紙1シートから {len(df_final)} 行のデータを正常に成形しました。")
    return df_final

def update_google_sheet(df, spreadsheet_id):
    print("--- Googleスプレッドシートへの書き込み ---")
    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ]
    
    # GitHubのSecretから認証情報を取得
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_json:
        print("エラー: GOOGLE_CREDENTIALS が設定されていません。")
        return
        
    import json
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    
    # スプレッドシートを開き、最初のシート（シート1）に追記
    sh = gc.open_by_key(spreadsheet_id)
    worksheet = sh.get_worksheet(0)
    
    # スプレッドシートへ追加（ヘッダーなしでデータ行のみ追記する場合は values = df.values.tolist() ）
    values = [df.columns.values.tolist()] + df.values.tolist()
    worksheet.append_rows(values, value_input_option='USER_ENTERED')
    print("書き込みが完了しました。")

if __name__ == "__main__":
    SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
    
    if SPREADSHEET_ID:
        print(f"【確認】SPREADSHEET_ID は正常に読み込まれています: {SPREADSHEET_ID[:5]}...")
    else:
        print("【警告】SPREADSHEET_ID が取得できませんでした。")
    
    date_str = get_target_date()
    df_oi, df_tp = download_jpx_data(date_str)
    
    # データがどちらも取得できなかったらその時点で終了
    if df_oi is None and df_tp is None:
        print("【判定】データがどちらも取得できなかったため、処理を終了します。")
        import sys
        sys.exit(1)

    # データの加工・成形
    df_final = process_data(df_oi, df_tp)
    
    # スプレッドシートへの書き込み（IDと認証情報の両方がある場合のみ実行）
    if SPREADSHEET_ID and os.environ.get("GOOGLE_CREDENTIALS"):
        try:
            update_google_sheet(df_final, SPREADSHEET_ID)
            print("【成功】すべての処理が正常に完了し、スプレッドシートに書き込まれました！")
        except Exception as e:
            print(f"【エラー】スプレッドシートへの書き込み中に問題が発生しました: {e}")
            import sys
            sys.exit(1)
    else:
        print("【判定】SPREADSHEET_ID または GOOGLE_CREDENTIALS が設定されていないため、書き込みをスキップしました。")