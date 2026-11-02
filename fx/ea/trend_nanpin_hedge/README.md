# TrendNanpinHedgeEA (順張りナンピン + 逆方向で救済)

峻さんの手書き図と回答 (2026-11-02) をもとにした MT5 EA と、同じルールの Python バックテスト。

## ルール

| 項目 | 内容 |
|---|---|
| 初回エントリー | シグナルの方向に 0.01 ロット。シグナルは「EMA200 の向きに RSI 押し目」(既定)、「高値/安値ブレイク」、外部 (AI がグローバル変数 `TNH_SIGNAL_<シンボル>` に +1/-1 を書く) から選択 |
| ロジック1 順張りナンピン | 直前のエントリーから間隔だけ逆行するごとに 1 本追加。ロット ×1.5 (0.01→0.02→0.02→0.03→0.05)、最大 5 本 |
| ロジック2 救済 | 3 本目は逆方向。逆方向ポジションは 間隔×0.5 の利益で利確。4・5 本目の向きは選択 (既定: 元の方向) |
| 決済 | サイクル損益 (救済の確定利益 + 含み損益) が 間隔×0.2×0.01ロット相当で全決済 (基本 1〜2 本で決済)。4 本以上は最も逆行した位置から 間隔×0.5 戻ったら全決済 |
| 間隔 | 固定 pips か 日足ATR(14)×係数。既定は ATR×0.5 (金 2026 年で約 470pips、ユーロドルで約 40pips) |

金の 1 pip は 0.1 ドルとして扱う (`InpPipSize` で変更可)。

## ファイル

- `mql5/TrendNanpinHedgeEA.mq5` EA 本体。`mql5/*.set` は推奨設定とユーザー指定 (固定 50pips) の設定ファイル
- `tnh_bt.py` バックテスター (numba)、`signals.py` データ読み込みとシグナル、`tests/` 手計算テスト
- `study.py` 間隔などの総当たり (4,968 通り → `results/grid.csv`)、`compare.py` 比較表と資産曲線 (`results/compare.md`, `results/equity.png`)

## 再現

```
pip install -r requirements.txt
python3 fetch_data.py      # data/ にダウンロード (コミットしない)
python3 -m pytest -q tests
python3 study.py && python3 compare.py
```

## 注意

- MetaEditor でのコンパイルとストラテジーテスターでの確認はこの環境ではできていない。
- どの設定もはっきりした優位性は確認できていない (results/compare.md)。実口座の前にデモで確認すること。
