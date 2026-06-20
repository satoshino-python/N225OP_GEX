import os
import datetime
import requests
import pandas as pd
import gspread
import json
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

def process_tp_data(df_tp):
    print("--- 理論価格データの加工・成形（ose[日付]tp.csv） ---")
    
    if df_tp is None or df_tp.empty:
        print("警告: 理論価格データ(df_tp)がないため、加工をスキップします。")
        return pd.DataFrame()

    # 1. 添付ファイルの定義に基づきヘッダー（列名）を強制設定
    headers = [
        "商品コード", "商品タイプ", "限月", "権利行使価格", "予備",
        "銘柄コード_put", "終値_put", "予備_put", "理論価格_put", "ボラティリティ_put",
        "銘柄コード_call", "終値_call", "予備_call", "理論価格_call", "ボラティリティ_call",
        "原資産終値", "基準ボラティリティ"
    ]
    
    # 元データの列数がヘッダーと一致することを確認して割り当て
    if len(df_tp.columns) == len(headers):
        df_tp.columns = headers
    else:
        # 万が一ヘッダーなしでPandasが1行目を吸い上げて列数が1つズレている場合の安全処理
        df_tp = pd.read_csv(StringIO(df_tp.to_csv(header=False, index=False)), names=headers)

    # 2. 【条件1】商品コードが「NK225E」のものに絞る
    df_filtered = df_tp[df_tp["商品コード"].astype(str).str.strip() == "NK225E"].copy()
    if df_filtered.empty:
        print("⚠️ 商品コード 'NK225E' に合致するデータが見つかりませんでした。")
        return pd.DataFrame()

    # 型を安全に数値型（int / float）に変換
    df_filtered["限月"] = pd.to_numeric(df_filtered["限月"], errors='coerce')
    df_filtered["権利行使価格"] = pd.to_numeric(df_filtered["権利行使価格"], errors='coerce')
    df_filtered["原資産終値"] = pd.to_numeric(df_filtered["原資産終値"], errors='coerce')

    # 3. 【条件2】限月が最小値から3つに絞る
    unique_months = sorted(df_filtered["限月"].dropna().unique())
    target_months = unique_months[:3]  # 最小値から3つを取得
    print(f"対象とする限月（上位3つ）: {target_months}")
    df_filtered = df_filtered[df_filtered["限月"].isin(target_months)]

    # 4. 【条件3】権利行使価格が原資産終値から上下15％のものに絞る
    # ※1行目の原資産終値を基準に判定（通常はすべて同値ですが安全のため）
    underlying_price = df_filtered["原資産終値"].iloc[0]
    min_strike = underlying_price * 0.85
    max_strike = underlying_price * 1.15
    print(f"原資産終値: {underlying_price}円 (対象権利行使価格: {int(min_strike)}円 〜 {int(max_strike)}円)")
    
    df_filtered = df_filtered[
        (df_filtered["権利行使価格"] >= min_strike) & 
        (df_filtered["権利行使価格"] <= max_strike)
    ]

    # 5. 【条件4・5】プットとコールを縦に分解し、指定された5列＋種別、取得日の形にする
    # プットデータの切り出し
    put_cols = {
        "限月": "限月",
        "権利行使価格": "権利行使価格",
        "理論価格_put": "理論価格",
        "ボラティリティ_put": "ボラティリティ",
        "原資産終値": "原資産終値"
    }
    df_put = df_filtered[list(put_cols.keys())].rename(columns=put_cols).copy()
    df_put["プットコール種別"] = "put"

    # コールデータの切り出し
    call_cols = {
        "限月": "限月",
        "権利行使価格": "権利行使価格",
        "理論価格_call": "理論価格",
        "ボラティリティ_call": "ボラティリティ",
        "原資産終値": "原資産終値"
    }
    df_call = df_filtered[list(call_cols.keys())].rename(columns=call_cols).copy()
    df_call["プットコール種別"] = "call"

    # 縦に綺麗に結合
    df_tp_combined = pd.concat([df_put, df_call], ignore_index=True)

    # 取得日を付与（既存の共通仕様に合わせる）
    current_date = get_target_date()
    df_tp_combined["取得日"] = current_date

    # ご指定の列名および順序に並び替え（「取得日」も先頭に集約すると管理がラクです）
    final_cols = ["取得日", "プットコール種別", "限月", "権利行使価格", "理論価格", "ボラティリティ", "原資産終値"]
    df_final_tp = df_tp_combined[final_cols].copy()

    # 数値のクレンジング処理（理論価格やボラティリティのハイフン等を0に置換）
    df_final_tp["理論価格"] = pd.to_numeric(df_final_tp["理論価格"].astype(str).str.replace(r'[\s,]', '', regex=True).replace('-', '0'), errors='coerce').fillna(0).astype(float)
    df_final_tp["ボラティリティ"] = pd.to_numeric(df_final_tp["ボラティリティ"].astype(str).str.replace(r'[\s,]', '', regex=True).replace('-', '0'), errors='coerce').fillna(0).astype(float)
    df_final_tp["限月"] = df_final_tp["限月"].astype(int)
    df_final_tp["権利行使価格"] = df_final_tp["権利行使価格"].astype(int)

    print(f"加工完了: 理論価格データから合計 {len(df_final_tp)} 行のデータを成形しました。")
    return df_final_tp

def update_google_sheet(df, spreadsheet_id):
    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ]
    
    # 環境変数から取得した文字列（JSON）をそのままパースして認証
    google_creds_env = os.environ.get("GOOGLE_CREDENTIALS")
    creds_dict = json.loads(google_creds_env)
    credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    
    # gspreadを使用してスプレッドシートへ書き込み
    gc = gspread.authorize(credentials)
    sh = gc.open_by_key(spreadsheet_id)
    worksheet = sh.get_worksheet(0)
    
    # DataFrameをリストに変換して末尾に追記
    data_to_append = df.values.tolist()
    worksheet.append_rows(data_to_append)

if __name__ == "__main__":
    # 環境変数からダイレクトに取得
    SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
    GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS")
    
    # ログでのデバッグ確認
    if SPREADSHEET_ID:
        print(f"【デバッグ】SPREADSHEET_ID 検出: {SPREADSHEET_ID[:5]}...")
    else:
        print("【デバッグ】SPREADSHEET_ID が取得できていません。")
        
    if GOOGLE_CREDENTIALS:
        print(f"【デバッグ】GOOGLE_CREDENTIALS 検出: {len(GOOGLE_CREDENTIALS)} 文字のデータを認識しました。")
    else:
        print("【デバッグ】GOOGLE_CREDENTIALS が取得できていません。")
    
    # ターゲット日付の取得（テスト時は固定日付）
    date_str = get_target_date()
    
    # データのダウンロード（建玉残高Excel、理論価格CSVの両方を取得）
    df_oi, df_tp = download_jpx_data(date_str)
    
    if df_oi is None and df_tp is None:
        print("【判定】データがどちらも取得できなかったため、処理を終了します。")
        import sys
        sys.exit(1)

    # 1. 各データの加工・成形
    df_final_oi = process_data(df_oi, df_tp)
    df_final_tp = process_tp_data(df_tp)
    
    # 2. 2つのデータフレームの結合（マージ）
    df_merged = pd.DataFrame() # 初期化
    
    if (df_final_oi is not None and not df_final_oi.empty) and (df_final_tp is not None and not df_final_tp.empty):
        print("--- 2つのテーブルを結合します（インナーマージ） ---")
        df_final_oi["限月"] = pd.to_numeric("20" + df_final_oi["限月"].astype(str), errors='coerce').fillna(0).astype(int)
        join_keys = ["取得日", "プットコール種別", "限月", "権利行使価格"]
        df_merged = pd.merge(df_final_tp, df_final_oi, on=join_keys, how="inner")
        
        final_columns_order = [
            "取得日", "プットコール種別", "限月", "権利行使価格", 
            "理論価格", "ボラティリティ", "原資産終値", 
            "取引高", "当日建玉残高", "前日比", "前日建玉残高"
        ]
        df_merged = df_merged[final_columns_order].copy()
        print(f"結合完了: 条件に合致した {len(df_merged)} 行の統合データを生成しました。")

    # --------------------------------------------------
    # 📝 スプレッドシートへの書き込みフェーズ
    # --------------------------------------------------
    # 結合データが空でないことを最優先にチェックし、書き込み関数へそのまま流すロジックに変更
    if not df_merged.empty:
        print("→ 結合統合データが正常に生成されているため、書き込み処理を呼び出します。")
        try:
            # 認証の実際の成否は update_google_sheet 関数側で詳細に判定させます
            update_google_sheet(df_merged, SPREADSHEET_ID)
            print("【成功】すべての処理が正常に完了し、スプレッドシートに書き込まれました！")
        except Exception as e:
            print(f"【エラー】書き込み処理中に例外が発生しました: {e}")
            import sys
            sys.exit(1)
    else:
        print("【スキップ】結合データ(df_merged)が空のため、書き込み処理を行いませんでした。")