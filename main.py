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
import numpy as np
from scipy.stats import norm  # 🌟 グリークス計算に必要な標準正規分布をインポート

# 📊 グラフ描画用のライブラリを追加
import matplotlib
matplotlib.use('Agg')  # GUIのないサーバー環境（GitHub Actions等）でも動くように設定
import matplotlib.pyplot as plt

# 🌟 Streamlitをインポート
import streamlit as st

# 日本語文字化け対策（入っていない場合はフォント設定をマニュアル指定にフォールバック）
try:
    import japanize_matplotlib
except ImportError:
    pass

def get_target_date():
    # === 【本番用】自動で当日のJST日付を取得する場合 ===
    # 1. まずUTCの現在時刻をタイムゾーン付きで安全に取得
    utc_now = datetime.datetime.now(datetime.timezone.utc)
    # 2. そこに時差9時間を加算して日本時間（JST）にする
    jst_now = utc_now + datetime.timedelta(hours=9)
    return jst_now.strftime("%Y%m%d")

    # === 【テスト用】過去の特定日付を指定したい場合は以下を有効にしてください ===
    # return "20260622"

def download_jpx_data(date_str):
    print(f"--- JPXデータ取得開始: {date_str} ---")
    oi_url = f"https://www.jpx.co.jp/markets/derivatives/trading-volume/tvdivq00000014nn-att/{date_str}open_interest.xlsx"
    tp_url = f"https://www.jpx.co.jp/automation/markets/derivatives/option-price/files/ose{date_str}tp.csv"
    settlement_url = f"https://www.jpx.co.jp/markets/derivatives/settlement-price/tvdivq00000014l6-att/rb{date_str}.csv"
    
    headers = {"User-Agent": "Mozilla/5.0"}
    df_oi, df_tp, df_settle = None, None, None
    
    res_oi = requests.get(oi_url, headers=headers)
    if res_oi.status_code == 200:
        try:
            df_oi = pd.read_excel(BytesIO(res_oi.content), sheet_name="別紙1")
            print("建玉残高（別紙1シート）の取得に成功しました。")
        except Exception as e:
            print(f"エラー: Excelのシート「別紙1」の読み込みに失敗しました: {e}")
        
    res_tp = requests.get(tp_url, headers=headers)
    if res_tp.status_code == 200:
        try:
            content = res_tp.content.decode('utf-8')
        except UnicodeDecodeError:
            content = res_tp.content.decode('shift_jis')
        
        df_tp = pd.read_csv(StringIO(content))
        print("理論価格の取得に成功しました。")
        
    res_settle = requests.get(settlement_url, headers=headers)
    if res_settle.status_code == 200:
        try:
            content = res_settle.content.decode('utf-8')
        except UnicodeDecodeError:
            content = res_settle.content.decode('shift_jis')
        df_settle = pd.read_csv(StringIO(content), header=None)
        print("清算数値（グリークス用インプット）の取得に成功しました。")
    
    return df_oi, df_tp, df_settle

def extract_greeks_inputs(df_settle):
    print("--- グリークス計算用インプットデータの抽出 ---")
    if df_settle is None or df_settle.empty:
        return pd.DataFrame()

    try:
        df_work = df_settle.copy()
        if df_work.shape[1] <= 11:
            return pd.DataFrame()
            
        df_work[1] = df_work[1].astype(str).str.strip()
        df_work[11] = df_work[11].astype(str).str.strip()
        
        df_filtered = df_work[(df_work[11] == "日経225") & (df_work[1].str.startswith("FUT_225"))].copy()
        if df_filtered.empty:
            return pd.DataFrame()

        df_filtered[3] = pd.to_numeric(df_filtered[3], errors='coerce')
        df_filtered[7] = pd.to_numeric(df_filtered[7], errors='coerce')
        df_filtered[9] = pd.to_numeric(df_filtered[9], errors='coerce')
        df_filtered[10] = pd.to_numeric(df_filtered[10], errors='coerce')
      
        df_filtered = df_filtered.dropna(subset=[3, 7, 9, 10])

        unique_months = sorted(df_filtered[3].unique(), reverse=False)
        target_months = unique_months[:3]
        df_filtered = df_filtered[df_filtered[3].isin(target_months)]

        df_filtered["調整残存日数"] = df_filtered[10] - 1
        df_filtered["調整残存日数"] = df_filtered["調整残存日数"].clip(lower=0)

        df_inputs = pd.DataFrame({
            "限月": df_filtered[3].astype(int),
            "原資産価格_S": df_filtered[7].astype(float),
            "金利_r": df_filtered[9].astype(float),
            "残存日数_D": df_filtered["調整残存日数"].astype(int)
        })

        df_inputs = df_inputs.drop_duplicates(subset=["限月"]).reset_index(drop=True)
        return df_inputs
    except Exception as e:
        print(f"⚠️ インプットデータ抽出中にエラーが発生しました: {e}")
        return pd.DataFrame()

def calculate_greeks(row):
    S = row["原資産価格_S"]
    K = row["権利行使価格"]
    D = row["残存日数_D"]
    r = row["金利_r"] / 100.0
    v = row["ボラティリティ"] 
    option_type = row["プットコール種別"]
    
    if D <= 0 or v <= 0:
        return pd.Series([0.0, 0.0, 0.0, 0.0])
        
    T = D / 365.0
    
    d1 = (np.log(S / K) + (r + 0.5 * v ** 2) * T) / (v * np.sqrt(T))
    d2 = d1 - v * np.sqrt(T)
    
    pdf_d1 = norm.pdf(d1)
    cdf_d1 = norm.cdf(d1)
    cdf_d2 = norm.cdf(d2)
    
    if option_type == "call":
        delta = cdf_d1
    else:
        delta = cdf_d1 - 1.0
        
    gamma = pdf_d1 / (S * v * np.sqrt(T))
    vega = (S * np.sqrt(T) * pdf_d1) / 100.0
    
    if option_type == "call":
        theta = (- (S * v * pdf_d1) / (2 * np.sqrt(T)) - r * K * np.exp(-r * T) * cdf_d2) / 365.0
    else:
        theta = (- (S * v * pdf_d1) / (2 * np.sqrt(T)) + r * K * np.exp(-r * T) * norm.cdf(-d2)) / 365.0
        
    return pd.Series([delta, gamma, vega, theta])

def process_data(df_oi, df_tp):
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
    if df_tp is None or df_tp.empty:
        return pd.DataFrame()
    headers = [
        "商品コード", "商品タイプ", "限月", "権利行使価格", "予備",
        "銘柄コード_put", "終値_put", "予備_put", "理論価格_put", "ボラティリティ_put",
        "銘柄コード_call", "終値_call", "予備_call", "理論価格_call", "ボラティリティ_call",
        "原資産終値", "基準ボラティリティ"
    ]
    if len(df_tp.columns) != len(headers):
        df_tp = pd.read_csv(StringIO(df_tp.to_csv(header=False, index=False)), names=headers)
    else:
        df_tp.columns = headers

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

# 🌟 変更箇所：引数に「選択された対象(selected_target)」を受け取り、Streamlit上でグラフを描画・出力する仕組みに変更
def generate_gex_plots(df_merged, selected_target):
    print(f"--- ガンマエクスポージャー（GEX）のグラフを生成します: {selected_target} ---")
    unique_months = sorted(df_merged["限月"].unique())
    
    if selected_target == "直近3限月合計":
        df_plot = df_merged.copy()
        title_str = "直近3限月合計"
        # 合算時の基準価格は、直近限月の原資産価格を代表値として使用
        underlying_price = df_merged[df_merged["限月"] == unique_months[0]]["原資産価格_S"].iloc[0]
    else:
        df_plot = df_merged[df_merged["限月"] == int(selected_target)].copy()
        title_str = f"限月: {selected_target}"
        underlying_price = df_plot["原資産価格_S"].iloc[0]
        
    gex_summary = df_plot.groupby("権利行使価格")["GEX(億円)"].sum().sort_index()
    
    if gex_summary.empty:
        st.warning("表示するデータがありません。")
        return
        
    fig, ax = plt.subplots(figsize=(12, 6))
    colors = ['#1f77b4' if val >= 0 else '#d62728' for val in gex_summary.values]
    
    ax.bar(gex_summary.index, gex_summary.values, color=colors, width=200, edgecolor='black', alpha=0.8)
    ax.axvline(x=underlying_price, color='green', linestyle='--', linewidth=1.5, label=f'原資産価格 (先物代表): {underlying_price:,.0f}')
    
    ax.set_title(f"日経225オプション ガンマエクスポージャー (GEX) - {title_str}", fontsize=14, fontweight='bold')
    ax.set_xlabel("権利行使価格 (Strike)", fontsize=12)
    ax.set_ylabel("GEX (億円 / 原資産1%変動あたり)", fontsize=12)
    ax.grid(True, linestyle=':', alpha=0.6)
    ax.legend(loc="upper left")
    ax.set_xlim(underlying_price * 0.90, underlying_price * 1.10)
    
    plt.tight_layout()
    
    # 🌟 Streamlitのウェブページ上にグラフを出力
    st.pyplot(fig)
    plt.close(fig)

def update_google_sheet(df, spreadsheet_id):
    scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
    google_creds_env = os.environ.get("GOOGLE_CREDENTIALS")
    creds_dict = json.loads(google_creds_env)
    credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(credentials)
    sh = gc.open_by_key(spreadsheet_id)
    worksheet = sh.get_worksheet(0)
    
    existing_data = worksheet.get_all_values()
    
    is_sheet_empty = True
    if existing_data:
        for row in existing_data:
            if len("".join(row).strip()) > 0:
                is_sheet_empty = False
                break

    data_to_append = df.values.tolist()
    
    if is_sheet_empty:
        header = [df.columns.tolist()]
        worksheet.append_rows(header + data_to_append)
        print("シートが空であることを確認したため、ヘッダーとデータを書き込みました。")
    else:
        worksheet.append_rows(data_to_append)
        print("既存のデータがあるため、データのみを追記しました。")

if __name__ == "__main__":
    if "pip" in sys.argv: pass 
    
    # 🌟 Streamlitアプリのタイトル
    st.title("日経225オプション GEXアナリティクス")
    
    SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
    GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS")
    
    date_str = get_target_date()
    df_oi, df_tp, df_settle = download_jpx_data(date_str)
    
    if df_oi is None and df_tp is None:
        st.error("【判定】データが取得できなかったため、処理を終了します。")
        sys.exit(1)

    df_greeks_inputs = extract_greeks_inputs(df_settle)

    df_final_oi = process_data(df_oi, df_tp)
    df_final_tp = process_tp_data(df_tp)
    
    df_merged = pd.DataFrame()
    
    if (df_final_oi is not None and not df_final_oi.empty) and (df_final_tp is not None and not df_final_tp.empty):
        print("--- 2つのテーブルを結合します（インナーマージ） ---")
        df_final_oi["限月"] = pd.to_numeric("20" + df_final_oi["限月"].astype(str), errors='coerce').fillna(0).astype(int)
        join_keys = ["取得日", "プットコール種別", "限月", "権利行使価格"]
        df_merged = pd.merge(df_final_tp, df_final_oi, on=join_keys, how="inner")
        
        if not df_greeks_inputs.empty:
            print("--- グリークス計算インプットを結合し、Black-Scholes指標を算出します ---")
            df_merged = pd.merge(df_merged, df_greeks_inputs, on=["限月"], how="inner")
            
            df_merged[["デルタ", "ガンマ", "ベガ", "セータ"]] = df_merged.apply(calculate_greeks, axis=1)
            
            df_merged["GEX符号"] = df_merged["プットコール種別"].map({"call": 1.0, "put": -1.0})
            df_merged["GEX_raw"] = df_merged["ガンマ"] * df_merged["当日建玉残高"] * 1000 * df_merged["原資産価格_S"] * 0.01 * df_merged["GEX符号"]
            df_merged["GEX(億円)"] = df_merged["GEX_raw"] / 100000000.0
            
            # 🌟 変更箇所：Streamlitのプルダウン（セレクトボックス）を作成し、選択された対象をグラフ生成関数に渡す
            unique_months = sorted(df_merged["限月"].unique())
            options = [str(m) for m in unique_months] + ["直近3限月合計"]
            
            selected_target = st.selectbox("表示する限月を選択してください", options, index=len(options)-1)
            generate_gex_plots(df_merged, selected_target)
            
            df_merged["原資産終値"] = df_merged["原資産価格_S"]
            
            final_columns_order = [
                "取得日", "プットコール種別", "限月", "権利行使価格", 
                "理論価格", "ボラティリティ", "原資産終値", 
                "取引高", "当日建玉残高", "前日比", "前日建玉残高",
                "デルタ", "ガンマ", "ベガ", "セータ", "GEX(億円)"
            ]
            df_merged = df_merged[final_columns_order].copy()
            print(f"グリークス・GEX計算完了: {len(df_merged)} 行の拡張データを生成しました。")
        else:
            print("⚠️ グリークスインプットが空のため、計算をスキップして基本項目のみで続行します。")
            final_columns_order = [
                "取得日", "プットコール種別", "限月", "権利行使価格", 
                "理論価格", "ボラティリティ", "原資産終値", 
                "取引高", "当日建玉残高", "前日比", "前日建玉残高"
            ]
            df_merged = df_merged[final_columns_order].copy()

    # 📝 スプレッドシートへの書き込みフェーズ
    if not df_merged.empty:
        print("→ 統合データ（グリークス・GEX付き）をスプレッドシートへ書き込みます。")
        try:
            update_google_sheet(df_merged, SPREADSHEET_ID)
            print("【成功】すべての処理が正常に完了し、スプレッドシートに書き込まれました！")
        except Exception as e:
            print(f"【エラー】書き込み処理中に例外が発生しました: {e}")
            sys.exit(1)
    else:
         print("【スキップ】結合データが空のため、書き込み処理をスキップしました。")