import streamlit as st
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import requests
from io import StringIO, BytesIO
import re
import datetime
from scipy.stats import norm

# ページの設定
st.set_page_config(page_title="日経225オプション GEXダッシュボード", layout="wide")

# --- main.pyのデータ処理ロジックを関数化して移植 ---
def get_target_date():
    utc_now = datetime.datetime.now(datetime.timezone.utc)
    jst_now = utc_now + datetime.timedelta(hours=9)
    return jst_now.strftime("%Y%m%d")

def download_jpx_data(date_str):
    oi_url = f"https://www.jpx.co.jp/markets/derivatives/trading-volume/tvdivq00000014nn-att/{date_str}open_interest.xlsx"
    tp_url = f"https://www.jpx.co.jp/automation/markets/derivatives/option-price/files/ose{date_str}tp.csv"
    settlement_url = f"https://www.jpx.co.jp/markets/derivatives/settlement-price/tvdivq00000014l6-att/rb{date_str}.csv"
    
    headers = {"User-Agent": "Mozilla/5.0"}
    df_oi, df_tp, df_settle = None, None, None
    
    res_oi = requests.get(oi_url, headers=headers)
    if res_oi.status_code == 200:
        try:
            df_oi = pd.read_excel(BytesIO(res_oi.content), sheet_name="別紙1")
        except Exception:
            pass
        
    res_tp = requests.get(tp_url, headers=headers)
    if res_tp.status_code == 200:
        try: content = res_tp.content.decode('utf-8')
        except UnicodeDecodeError: content = res_tp.content.decode('shift_jis')
        df_tp = pd.read_csv(StringIO(content))
        
    res_settle = requests.get(settlement_url, headers=headers)
    if res_settle.status_code == 200:
        try: content = res_settle.content.decode('utf-8')
        except UnicodeDecodeError: content = res_settle.content.decode('shift_jis')
        df_settle = pd.read_csv(StringIO(content), header=None)
        
    return df_oi, df_tp, df_settle

def extract_greeks_inputs(df_settle):
    if df_settle is None or df_settle.empty: return pd.DataFrame()
    try:
        df_work = df_settle.copy()
        df_work[1] = df_work[1].astype(str).str.strip()
        df_work[11] = df_work[11].astype(str).str.strip()
        df_filtered = df_work[(df_work[11] == "日経225") & (df_work[1].str.startswith("FUT_225"))].copy()
        df_filtered[3] = pd.to_numeric(df_filtered[3], errors='coerce')
        df_filtered[7] = pd.to_numeric(df_filtered[7], errors='coerce')
        df_filtered[9] = pd.to_numeric(df_filtered[9], errors='coerce')
        df_filtered[10] = pd.to_numeric(df_filtered[10], errors='coerce')
        df_filtered = df_filtered.dropna(subset=[3, 7, 9, 10])
        unique_months = sorted(df_filtered[3].unique())[:3]
        df_filtered = df_filtered[df_filtered[3].isin(unique_months)]
        df_filtered["調整残存日数"] = (df_filtered[10] - 1).clip(lower=0)
        df_inputs = pd.DataFrame({
            "限月": df_filtered[3].astype(int),
            "原資産価格_S": df_filtered[7].astype(float),
            "金利_r": df_filtered[9].astype(float),
            "残存日数_D": df_filtered["調整残存日数"].astype(int)
        })
        return df_inputs.drop_duplicates(subset=["限月"]).reset_index(drop=True)
    except Exception: return pd.DataFrame()

def calculate_greeks(row):
    S, K, D, v = row["原資産価格_S"], row["権利行使価格"], row["残存日数_D"], row["ボラティリティ"]
    r = row["金利_r"] / 100.0
    if D <= 0 or v <= 0: return pd.Series([0.0, 0.0, 0.0, 0.0])
    T = D / 365.0
    d1 = (np.log(S / K) + (r + 0.5 * v ** 2) * T) / (v * np.sqrt(T))
    d2 = d1 - v * np.sqrt(T)
    pdf_d1, cdf_d1 = norm.pdf(d1), norm.cdf(d1)
    delta = cdf_d1 if row["プットコール種別"] == "call" else cdf_d1 - 1.0
    gamma = pdf_d1 / (S * v * np.sqrt(T))
    vega = (S * np.sqrt(T) * pdf_d1) / 100.0
    if row["プットコール種別"] == "call":
        theta = (- (S * v * pdf_d1) / (2 * np.sqrt(T)) - r * K * np.exp(-r * T) * norm.cdf(d2)) / 365.0
    else:
        theta = (- (S * v * pdf_d1) / (2 * np.sqrt(T)) + r * K * np.exp(-r * T) * norm.cdf(-d2)) / 365.0
    return pd.Series([delta, gamma, vega, theta])

def process_data(df_oi):
    if df_oi is None or df_oi.empty: return pd.DataFrame()
    
    # 割り当てたい正しい列名の定義
    temp_columns = ["限月取引", "取引高", "当日建玉残高", "前日比", "前日建玉残高"]
    
    # main.py と同じ確実な列名上書き方法に修正
    df_put = df_oi.iloc[:, [0, 1, 2, 3, 4]].copy()
    df_put.columns = temp_columns
    
    df_call = df_oi.iloc[:, [6, 7, 8, 9, 10]].copy()
    df_call.columns = temp_columns
    
    # 結合して前後の空白を除去
    df_combined = pd.concat([df_put, df_call], ignore_index=True)
    
    df_combined["限月取引"] = df_combined["限月取引"].astype(str).str.strip()
    
    # 🌟 修正ポイント：'NIKKEI' を含み、かつ 'MINI' と '合計' を「含まない」行だけを厳密に抽出
    df_combined = df_combined[
        df_combined["限月取引"].str.contains("NIKKEI", na=False, case=False) & 
        ~df_combined["限月取引"].str.contains("MINI", na=False, case=False) & 
        ~df_combined["限月取引"].str.contains("合計", na=False)
    ]
    
    # 正規表現でスライス
    extracted = df_combined["限月取引"].str.extract(r"NIKKEI\s*225\s*([PC])(\d{4})-(\d+)", flags=re.IGNORECASE)
    df_combined["プットコール種別"] = extracted[0].str.upper().map({"P": "put", "C": "call"})
    df_combined["限月"] = extracted[1]
    df_combined["権利行使価格"] = extracted[2]
    df_combined = df_combined.dropna(subset=["プットコール種別", "限月", "権利行使価格"])
    
    # 数値型への一括変換
    num_cols = ["権利行使価格", "取引高", "当日建玉残高", "前日比", "前日建玉残高"]
    for col in num_cols:
        df_combined[col] = pd.to_numeric(df_combined[col].astype(str).str.replace(r'[\s,]', '', regex=True).replace('-', '0'), errors='coerce').fillna(0).astype(int)
        
    return df_combined.reset_index(drop=True)

def process_tp_data(df_tp):
    if df_tp is None or df_tp.empty: return pd.DataFrame()
    headers = ["商品コード", "商品タイプ", "限月", "権利行使価格", "予備", "銘柄コード_put", "終値_put", "予備_put", "理論価格_put", "ボラティリティ_put", "銘柄コード_call", "終値_call", "予備_call", "理論価格_call", "ボラティリティ_call", "原資産終値", "基準ボラティリティ"]
    df_tp.columns = headers if len(df_tp.columns) == len(headers) else df_tp.columns
    df_filtered = df_tp[df_tp["商品コード"].astype(str).str.strip() == "NK225E"].copy()
    df_filtered["限月"] = pd.to_numeric(df_filtered["限月"], errors='coerce')
    df_filtered["権利行使価格"] = pd.to_numeric(df_filtered["権利行使価格"], errors='coerce')
    df_filtered["原資産終値"] = pd.to_numeric(df_filtered["原資産終値"], errors='coerce')
    target_months = sorted(df_filtered["限月"].dropna().unique())[:3]
    df_filtered = df_filtered[df_filtered["限月"].isin(target_months)]
    
    underlying = df_filtered["原資産終値"].iloc[0]
    df_filtered = df_filtered[(df_filtered["権利行使価格"] >= underlying * 0.85) & (df_filtered["権利行使価格"] <= underlying * 1.15)]
    
    df_p = df_filtered[["限月", "権利行使価格", "理論価格_put", "ボラティリティ_put", "原資産終値"]].rename(columns={"理論価格_put": "理論価格", "ボラティリティ_put": "ボラティリティ"}).copy()
    df_p["プットコール種別"] = "put"
    df_c = df_filtered[["限月", "権利行使価格", "理論価格_call", "ボラティリティ_call", "原資産終値"]].rename(columns={"理論価格_call": "理論価格", "ボラティリティ_call": "ボラティリティ"}).copy()
    df_c["プットコール種別"] = "call"
    
    df_res = pd.concat([df_p, df_c], ignore_index=True)
    df_res["理論価格"] = pd.to_numeric(df_res["理論価格"].astype(str).str.replace(r'[\s,]', '', regex=True).replace('-', '0'), errors='coerce').fillna(0).astype(float)
    df_res["ボラティリティ"] = pd.to_numeric(df_res["ボラティリティ"].astype(str).str.replace(r'[\s,]', '', regex=True).replace('-', '0'), errors='coerce').fillna(0).astype(float)
    df_res["限月"] = df_res["限月"].astype(int)
    df_res["権利行使価格"] = df_res["権利行使価格"].astype(int)
    return df_res

# 🌟 データを一気につくって df_merged を返却するメイン関数
# 🌟 データを一気につくって df_merged と「確定したデータ基準日」を返却するメイン関数
@st.cache_data(ttl=3600)  # 1時間キャッシュして、アクセス毎のJPX負荷を減らす
def load_and_calculate_all_data():
    # 1. ベースとなる本日の日付（JST）を取得
    utc_now = datetime.datetime.now(datetime.timezone.utc)
    jst_now = utc_now + datetime.timedelta(hours=9)
    
    df_oi_raw, df_tp_raw, df_settle_raw = None, None, None
    confirmed_date_str = ""
    
    # 2. 最大5日前まで自動で遡ってデータを探すループ
    for i in range(5):
        target_date = jst_now - datetime.timedelta(days=i)
        date_str = target_date.strftime("%Y%m%d")
        
        # 土日はJPXのデータ更新がないため最初からスキップして高速化
        if target_date.weekday() in [5, 6]:  # 5=土曜, 6=日曜
            continue
            
        # JPXから3つのデータをダウンロード試行
        df_oi_raw, df_tp_raw, df_settle_raw = download_jpx_data(date_str)
        
        # 3つのデータがすべて正常に揃ったら、その日付で確定してループを抜ける
        if df_oi_raw is not None and df_tp_raw is not None and df_settle_raw is not None:
            confirmed_date_str = target_date.strftime("%Y/%m/%d")
            break
            
    # 全て遡ってもデータが1つも見つからなかった場合は空のデータフレームと空文字を返す
    if df_oi_raw is None or df_tp_raw is None or df_settle_raw is None:
        return pd.DataFrame(), ""
        
    # 3. 確定したデータを使用したパース・計算処理
    df_greeks_inputs = extract_greeks_inputs(df_settle_raw)
    df_final_oi = process_data(df_oi_raw)
    df_final_tp = process_tp_data(df_tp_raw)
    
    if df_final_oi.empty or df_final_tp.empty:
        return pd.DataFrame(), ""
        
    # 建玉データと理論価格データのインナーマージ
    df_final_oi["限月"] = pd.to_numeric("20" + df_final_oi["限月"].astype(str), errors='coerce').fillna(0).astype(int)
    df_merged = pd.merge(df_final_tp, df_final_oi, on=["プットコール種別", "限月", "権利行使価格"], how="inner")
    
    # グリークス計算インプットの結合とBlack-Scholes指標の算出
    if not df_greeks_inputs.empty:
        df_merged["限月"] = df_merged["限月"].astype(int)
        df_greeks_inputs["限月"] = df_greeks_inputs["限月"].astype(int)
        df_merged = pd.merge(df_merged, df_greeks_inputs, on=["限月"], how="inner")
        
        if not df_merged.empty:
            # 各行に対して一括計算を適用（デルタ、ガンマ、ベガ、セータ）
            df_merged[["デルタ", "ガンマ", "ベガ", "セータ"]] = df_merged.apply(calculate_greeks, axis=1)
            
            # ガンマエクスポージャー（GEX）の計算
            # マーケットメーカー売り越し前提：Callはプラス、Putはマイナス符号を乗じる
            df_merged["GEX符号"] = df_merged["プットコール種別"].map({"call": 1.0, "put": -1.0})
            df_merged["GEX_raw"] = df_merged["ガンマ"] * df_merged["当日建玉残高"] * 1000 * df_merged["原資産価格_S"] * 0.01 * df_merged["GEX符号"]
            df_merged["GEX(億円)"] = df_merged["GEX_raw"] / 100000000.0
            
            # 計算済みのデータフレームと確定した日付文字列をセットで返却
            return df_merged, confirmed_date_str
            
    return pd.DataFrame(), ""

# --- 🚀 Streamlit 画面表示フェーズ ---
st.title("📊 日経225オプション ガンマエクスポージャー (GEX) ダッシュボード")

# 1. データの読み込み
with st.spinner("JPXから最新データを取得し、GEXを計算中..."):
    result = load_and_calculate_all_data()

# データの受け取りと安全チェック
if result is None or not isinstance(result, tuple) or len(result) < 2:
    st.error("⚠️ データの初期化に失敗しました。アプリのキャッシュをクリアするか再起動してください。")
else:
    df_merged, data_date = result

    # 🌟 修正ポイント：過去5日遡っても本当にデータが1件もない場合のみエラーを出す
    if df_merged is None or df_merged.empty or not data_date:
        st.error("❌ 直近5日分のデータがJPX（日本取引所グループ）側で見つかりませんでした。")
        st.info("土日祝日やシステムメンテナンス、またはURL構造の変更が発生している可能性があります。時間をおいて再度お試しください。")
    else:
        # 2. 限月選択ボックスの配置
        unique_months = sorted(df_merged["限月"].unique())
        selected_month = st.sidebar.selectbox("表示する限月を選択してください", unique_months)
        
        # 🌟 サイドバーに取得できた最新データの基準日を分かりやすく明示
        st.sidebar.info(f"📅 データ基準日: {data_date}")
        
        # 選択された限月のデータを抽出
        df_month = df_merged[df_merged["限月"] == selected_month].copy()
        gex_summary = df_month.groupby("権利行使価格")["GEX(億円)"].sum().sort_index()
        underlying_price = df_month["原資産価格_S"].iloc[0]
        
        # 3. 画面上に現在の主要な数値を配置
        col1, col2, col3 = st.columns(3)
        col1.metric("データ基準日", data_date)  # 今日がなければ前営業日の日付が自動でここに入ります
        col2.metric("基準原資産価格 (先物決済値)", f"{underlying_price:,.1f} 円")
        col3.metric("総データ行数 (Strike数)", f"{len(gex_summary)} 行")

        # 4. Plotlyによるインタラクティブなグラフ描画
        st.subheader(f"📈 ガンマエクスポージャープロット - {selected_month} 限月 ({data_date} 基準)")
        
        # データの整形
        df_plot = df_month.copy()
        
        # 権利行使価格（Strike）ごとにGEXやガンマ、デルタを集計
        df_grouped = df_plot.groupby("権利行使価格").agg({
            "GEX(億円)": "sum",
            "ガンマ": "mean",     
            "デルタ": "mean",     
            "当日建玉残高": "sum" 
        }).reset_index()
        
        # GEX（億円）とガンマを10,000倍に変換
        df_grouped["GEX (億円×1万)"] = df_grouped["GEX(億円)"] * 10000
        df_grouped["ガンマ (1万倍)"] = df_grouped["ガンマ"] * 10000
        df_grouped["方向"] = df_grouped["GEX (億円×1万)"].apply(lambda x: "Call優勢 (Long Gamma)" if x >= 0 else "Put優勢 (Short Gamma)")
        
        import plotly.express as px
        
        fig = px.bar(
            df_grouped,
            x="権利行使価格",
            y="GEX (億円×1万)",
            color="方向",
            color_discrete_map={"Call優勢 (Long Gamma)": "#1f77b4", "Put優勢 (Short Gamma)": "#d62728"},
            labels={"権利行使価格": "権利行使価格 (Strike)", "GEX (億円×1万)": "GEX (億円 × 1万)"},
            hover_data={
                "権利行使価格": ":,d",
                "GEX (億円×1万)": ":+,.0f",  
                "当日建玉残高": ":,d",
                "デルタ": ":,.2f",
                "ガンマ (1万倍)": ":,.2f",
                "方向": False
            }
        )
        
        # グラフのレイアウト調整
        fig.update_layout(
            xaxis_range=[underlying_price * 0.90, underlying_price * 1.10],
            hovermode="x unified",
            showlegend=True,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            margin=dict(l=20, r=20, t=30, b=20),
            plot_bgcolor="rgba(0,0,0,0)"
        )
        
        fig.update_xaxes(showgrid=True, gridwidth=1, gridcolor='LightPink', dtick=500)
        fig.update_yaxes(showgrid=True, gridwidth=1, gridcolor='LightGray')
        
        fig.add_vline(
            x=underlying_price, 
            line_width=2, 
            line_dash="dash", 
            line_color="green",
            annotation_text=f"原資産: {underlying_price:,.0f}円",
            annotation_position="top left"
        )
        
        st.plotly_chart(fig, use_container_width=True)
        
        # 5. 下部に生データテーブルを表示
        st.subheader("📋 算出データ詳細テーブル")
        
        df_month_display = df_month.copy()
        df_month_display["GEX (億円×1万)"] = df_month_display["GEX(億円)"] * 10000
        df_month_display["ガンマ (1万倍)"] = df_month_display["ガンマ"] * 10000
        
        show_cols = ["プットコール種別", "権利行使価格", "理論価格", "ボラティリティ", "当日建玉残高", "デルタ", "ガンマ (1万倍)", "GEX (億円×1万)"]
        st.dataframe(df_month_display[show_cols].sort_values(by="権利行使価格"), use_container_width=True)