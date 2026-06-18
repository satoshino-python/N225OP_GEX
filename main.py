import os
import datetime
import requests
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from io import StringIO, BytesIO

def get_target_date():
    # 実行時のJST（日本時間）の日付を取得（GitHub Actionsは標準でUTCのため+9時間する）
    jst_now = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    return jst_now.strftime("%Y%m%d")

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
        df_oi = pd.read_excel(BytesIO(res_oi.content))
        print("建玉残高の取得に成功しました。")
    else:
        print(f"建玉残高の取得に失敗 (Status: {res_oi.status_code})")
        
    # 2. 理論価格 (CSV) のダウンロード
    res_tp = requests.get(tp_url, headers=headers)
    if res_tp.status_code == 200:
        # JPXのCSVは文字コードがShift_JISの場合が多いため考慮
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
    print("--- データの加工・成形 ---")
    
    # TODO: ここに詳細なフィルタリングや結合ロジックを記述します。
    # 例：日経225オプションのみ抽出、特定の権利行使価格のみ抽出、等
    
    # 今回はサンプルとして、取得成功のログとデータの一部をスプレッドシートに投げる形にします
    processed_records = [
        {
            "取得日時": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ステータス": "成功",
            "建玉データ行数": len(df_oi) if df_oi is not None else 0,
            "理論価格データ行数": len(df_tp) if df_tp is not None else 0
        }
    ]
    return pd.DataFrame(processed_records)

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