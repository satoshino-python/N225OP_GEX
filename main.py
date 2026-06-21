import os
import datetime
import requests
import pandas as pd
import gspread
import json
from google.oauth2.service_account import Credentials
from io import StringIO, BytesIO
import re
import sys

def get_target_date():
    # === 【テスト用】任意の日付を指定する場合 ===
    return "20260619"

    # === 【本番用】自動で当日のJST日付を取得する場合 ===
    # jst_now = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    # return jst_now.strftime("%Y%m%d")

def download_jpx_data(date_str):
    print(f"--- JPXデータ取得開始: {date_str} ---")
    
    oi_url = f"https://www.jpx.co.jp/markets/derivatives/trading-volume/tvdivq00000014nn-att/{date_str}open_interest.xlsx"
    tp_url = f"https://www.jpx.co.jp/automation/markets/derivatives/option-price/files/ose{date_str}tp.csv"
    # 新規追加：基準価格・清算数値データURL
    settlement_url = f"https://www.jpx.co.jp/markets/derivatives/settlement-price/tvdivq00000014l6-att/rb{date_str}.csv"
    
    headers = {"User-Agent": "Mozilla/5.0"}
    df_oi, df_tp, df_settle = None, None, None
    
    # 1. 建玉残高 (Excel) のダウンロード
    res_oi = requests.get(oi_url, headers=headers)
    if res_oi.status_code == 200:
        try:
            df_oi = pd.read_excel(BytesIO(res_oi.content), sheet_name="別紙1")
            print("建玉残高（別紙1シート）の取得に成功しました。")
        except Exception as e:
            print(f"エラー: Excelのシート「別紙1」の読み込みに失敗しました: {e}")
        
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

    # 3. 清算数値 (CSV) のダウンロード 【新規実装】
    res_settle = requests.get(settlement_url, headers=headers)
    if res_settle.status_code == 200:
        try:
            content = res_settle.content.decode('utf-8')
        except UnicodeDecodeError:
            content = res_settle.content.decode('shift_jis')
        # ヘッダーが特殊な場合があるため、明示的に列名なしで読み込み後で制御可能にします
        df_settle = pd.read_csv(StringIO(content), header=None)
        print("清算数値（グリークス用インプット）の取得に成功しました。")
    else:
        print(f"清算数値の取得に失敗 (Status: {res_settle.status_code})")
        
    return df_oi, df_tp, df_settle

def extract_greeks_inputs(df_settle):
    """
    清算数値CSVからグリークス計算に必要なパラメータを抽出する関数 【新規実装】
    """
    print("--- グリークス計算用インプットデータの抽出 ---")
    if df_settle is None or df_settle.empty:
        print("警告: 清算数値データがないため、インプット抽出をスキップします。")
        return pd.DataFrame()

    try:
        # ご指定の列定義に合わせたマッピング
        # D列:3(限月), H列:7(原資産価格), J列:9(金利), K列:10(残存日数), L列:11(商品区分)
        df_work = df_settle.copy()
        
        # 1. L列が「日経225」のものを抽出
        # 列数が足りない場合の安全弁
        if df_work.shape[1] <= 11:
            print("⚠️ CSVの列数が不足しています。")
            return pd.DataFrame()
            
        # 文字列型に変換して前後の空白を削除
        df_work[1] = df_work[1].astype(str).str.strip() # B列
        df_work[11] = df_work[11].astype(str).str.strip() # L列
        
        # 🌟 【条件追加】L列が「日経225」かつ、B列が「FUT_225」で始まる行を抽出
        df_filtered = df_work[
            (df_work[11] == "日経225") & 
            (df_work[1].str.startswith("FUT_225"))
        ].copy()
        
        if df_filtered.empty:
            print("⚠️ L列 '日経225' かつ B列が 'FUT_225' で始まるデータが見つかりませんでした。")
            return pd.DataFrame()

        # 2. 型の安全な数値変換
        df_filtered[3] = pd.to_numeric(df_filtered[3], errors='coerce')   # 限月
        df_filtered[7] = pd.to_numeric(df_filtered[7], errors='coerce')   # 原資産価格
        df_filtered[9] = pd.to_numeric(df_filtered[9], errors='coerce')   # 金利
        df_filtered[10] = pd.to_numeric(df_filtered[10], errors='coerce') # 残存日数
        
        df_filtered = df_filtered.dropna(subset=[3, 7, 9, 10])

        # 3. D列の限月を直近3つに絞り込む
        unique_months = sorted(df_filtered[3].unique())
        target_months = unique_months[:3]
        print(f"インプット対象とする限月（上位3つ）: {target_months}")
        df_filtered = df_filtered[df_filtered[3].isin(target_months)]

        # 4. K列の残存日数から1を引いたものを残存日数とする
        df_filtered["調整残存日数"] = df_filtered[10] - 1
        # 負の数にならないよう下限を0に設定
        df_filtered["調整残存日数"] = df_filtered["調整残存日数"].clip(lower=0)

        # 必要な列だけを綺麗にまとめる
        df_inputs = pd.DataFrame({
            "限月": df_filtered[3].astype(int),
            "原資産価格": df_filtered[7].astype(float),
            "金利": df_filtered[9].astype(float),
            "残存日数": df_filtered["調整残存日数"].astype(int) # 1を引いた後の値
        })

        # 重複行を排除（限月ごとに一意のパラメータにする）
        df_inputs = df_inputs.drop_duplicates(subset=["限月"]).reset_index(drop=True)
        
        print("抽出した限月ごとのインプットパラメータ:")
        print(df_inputs.to_string(index=False))
        return df_inputs

    except Exception as e:
        print(f"⚠️ インプットデータ抽出中にエラーが発生しました: {e}")
        return pd.DataFrame()

def process_data(df_oi, df_tp):
    print("--- データの加工・成形（別紙1シート対応版） ---")
    if df_oi is None or df_oi.empty:
        return pd.DataFrame()

    df_clean = df_oi.copy()
    put_cols = [0, 1, 2, 3, 4]
    call_cols = [6, 7, 8, 9, 10]
    temp_columns = ["限月取引", "取引高", "当日建玉残高", "前日比", "前日建玉残高"]
    
    df_put = df_clean.iloc[:, put_cols].copy()
    df_put.columns = temp_columns
    df_call = df_clean.iloc[:, call_cols].copy()
    df_call.columns = temp_columns
    
    df_combined = pd.concat([df_put, df_call], ignore_index=True)
    df_combined["限月取引"] = df_combined["限月取引"].astype(str).str.strip()
    df_combined = df_combined[df_combined["限月取引"].str.contains("NIKKEI", na=False, case=False)]
    df_combined = df_combined[~df_combined["限月取引"].str.contains("合計", na=False)]
    
    if df_combined.empty:
        return pd.DataFrame()

    pattern = r"NIKKEI\s*225\s*([PC])(\d{4})-(\d+)"
    extracted = df_combined["限月取引"].str.extract(pattern, flags=re.IGNORECASE)
    
    df_combined["種別シグナル"] = extracted[0].str.upper()
    df_combined["限月"] = extracted[1]
    df_combined["権利行使価格"] = extracted[2]
    df_combined["プットコール種別"] = df_combined["種別シグナル"].map({"P": "put", "C": "call"})
    
    df_combined = df_combined.dropna(subset=["プットコール種別", "限月", "権利行使価格"])
    df_combined["取得日"] = get_target_date() 
    
    final_cols = ["取得日", "プットコール種別", "限月", "権利行使価格", "取引高", "当日建玉残高", "前日比", "前日建玉残高"]
    df_final = df_combined[final_cols].copy()
    
    num_cols = ["権利行使価格", "取引高", "当日建玉残高", "前日比", "前日建玉残高"]
    for col in num_cols:
        df_final[col] = df_final[col].astype(str).str.replace(r'[\s,]', '', regex=True)
        df_final[col] = pd.to_numeric(df_final[col].replace('-', '0').replace('nan', '0'), errors='coerce').fillna(0).astype(int)

    return df_final.reset_index(drop=True)

def process_tp_data(df_tp):
    print("--- 理論価格データの加工・成形（ose[日付]tp.csv） ---")
    if df_tp is None or df_tp.empty:
        return pd.DataFrame()

    headers = [
        "商品コード", "商品タイプ", "限月", "権利行使価格", "予備",
        "銘柄コード_put", "終値_put", "予備_put", "理論価格_put", "ボラティリティ_put",
        "銘柄コード_call", "終値_call", "予備_call", "理論価格_call", "ボラティリティ_call",
        "原資産終値", "基準ボラティリティ"
    ]
    
    if len(df_tp.columns) == len(headers):
        df_tp.columns = headers
    else:
        df_tp = pd.read_csv(StringIO(df_tp.to_csv(header=False, index=False)), names=headers)

    df_filtered = df_tp[df_tp["商品コード"].astype(str).str.strip() == "NK225E"].copy()
    if df_filtered.empty:
        return pd.DataFrame()

    df_filtered["限月"] = pd.to_numeric(df_filtered["限月"], errors='coerce')
    df_filtered["権利行使価格"] = pd.to_numeric(df_filtered["権利行使価格"], errors='coerce')
    df_filtered["原資産終値"] = pd.to_numeric(df_filtered["原資産終値"], errors='coerce')

    unique_months = sorted(df_filtered["限月"].dropna().unique())
    target_months = unique_months[:3]
    df_filtered = df_filtered[df_filtered["限月"].isin(target_months)]

    underlying_price = df_filtered["原資産終値"].iloc[0]
    min_strike = underlying_price * 0.85
    max_strike = underlying_price * 1.15
    
    df_filtered = df_filtered[(df_filtered["権利行使価格"] >= min_strike) & (df_filtered["権利行使価格"] <= max_strike)]

    put_cols = {"限月": "限月", "権利行使価格": "権利行使価格", "理論価格_put": "理論価格", "ボラティリティ_put": "ボラティリティ", "原資産終値": "原資産終値"}
    df_put = df_filtered[list(put_cols.keys())].rename(columns=put_cols).copy()
    df_put["プットコール種別"] = "put"

    call_cols = {"限月": "限月", "権利行使価格": "権利行使価格", "理論価格_call": "理論価格", "ボラティリティ_call": "ボラティリティ", "原資産終値": "原資産終値"}
    df_call = df_filtered[list(call_cols.keys())].rename(columns=call_cols).copy()
    df_call["プットコール種別"] = "call"

    df_tp_combined = pd.concat([df_put, df_call], ignore_index=True)
    df_tp_combined["取得日"] = get_target_date()

    final_cols = ["取得日", "プットコール種別", "限月", "権利行使価格", "理論価格", "ボラティリティ", "原資産終値"]
    df_final_tp = df_tp_combined[final_cols].copy()

    df_final_tp["理論価格"] = pd.to_numeric(df_final_tp["理論価格"].astype(str).str.replace(r'[\s,]', '', regex=True).replace('-', '0'), errors='coerce').fillna(0).astype(float)
    df_final_tp["ボラティリティ"] = pd.to_numeric(df_final_tp["ボラティリティ"].astype(str).str.replace(r'[\s,]', '', regex=True).replace('-', '0'), errors='coerce').fillna(0).astype(float)
    df_final_tp["限月"] = df_final_tp["限月"].astype(int)
    df_final_tp["権利行使価格"] = df_final_tp["権利行使価格"].astype(int)

    return df_final_tp

def update_google_sheet(df, spreadsheet_id):
    scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
    google_creds_env = os.environ.get("GOOGLE_CREDENTIALS")
    creds_dict = json.loads(google_creds_env)
    credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    
    gc = gspread.authorize(credentials)
    sh = gc.open_by_key(spreadsheet_id)
    worksheet = sh.get_worksheet(0)
    
    data_to_append = df.values.tolist()
    worksheet.append_rows(data_to_append)

if __name__ == "__main__":
    SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
    GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS")
    
    date_str = get_target_date()
    
    # 🌟 3つ目の戻り値として df_settle を受け取るように拡張
    df_oi, df_tp, df_settle = download_jpx_data(date_str)
    
    if df_oi is None and df_tp is None:
        print("【判定】必要な主要データが取得できなかったため、処理を終了します。")
        sys.exit(1)

    # 🌟 新規追加：グリークス計算に必要なインプットの抽出を実行
    df_greeks_inputs = extract_greeks_inputs(df_settle)

    # 1. 各データの加工・成形
    df_final_oi = process_data(df_oi, df_tp)
    df_final_tp = process_tp_data(df_tp)
    
    # 2. 2つのデータフレームの結合（マージ）
    df_merged = pd.DataFrame()
    
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
    # 🎯 次のステップ：ここで df_greeks_inputs を df_merged にマージし、
    # Black-Scholesモデルを用いてデルタ・ガンマ等の計算を行います。
    # --------------------------------------------------

    # 📝 スプレッドシートへの書き込みフェーズ
    if not df_merged.empty:
        print("→ 結合統合データが正常に生成されているため、書き込み処理を呼び出します。")
        try:
            update_google_sheet(df_merged, SPREADSHEET_ID)
            print("【成功】すべての処理が正常に完了し、スプレッドシートに書き込まれました！")
        except Exception as e:
            print(f"【エラー】書き込み処理中に例外が発生しました: {e}")
            sys.exit(1)
    else:
        print("【スキップ】結合データ(df_merged)が空のため、書き込み処理を行いませんでした。")