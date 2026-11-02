# TrendNanpinHedgeEA (順張りナンピン + 逆方向で救済)

峻さんの手書き図と回答 (2026-11-02) をもとにした MT5 / MT4 EA と、同じルールの Python バックテスト。

## 追加回答 (2026-11-02) で決まったこと

- 3 本目と 4 本目は逆方向、5 本目はなし (最大 4 本)
- MT4 と MT5 の両方、ゴールド 5 分足
- AI が判断する初回エントリー = CCI の 0 ライン抜け (上抜け買い / 下抜け売り)
- 「1 回の大負けを小さくする」を再検討: ロット倍率 1.0/1.2/1.5、1 サイクル損失上限、強トレンド時 (H1 ADX) は入らない、を 8,064 通り比較 (`study_risk.py` → `results/compare_risk.md`)

EA の初期値は「バランス型」(CCI14 / 固定 300pips / 倍率 1.5 / 損失上限 3 万円)。
`*_defensive.set` は「守り重視」(CCI20 / 500pips / 倍率 1.0 / 損失上限 0.75 万円 / ADX>25 では入らない)。
MT4 用の設定ファイルは `mql4/` (時間足の番号が MT5 と違うため別ファイル)。

以下は最初の版 (RSI 押し目, 最大 5 本) の説明。

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

- `mql5/TrendNanpinHedgeEA.mq5` / `mql4/TrendNanpinHedgeEA.mq4` EA 本体。`*.set` はバランス型・守り重視・ご指定 50pips
- `tnh_bt.py` バックテスター (numba)、`signals.py` データ読み込みとシグナル、`tests/` 手計算テスト
- `study_cci.py` CCI 版の間隔比較、`study_risk.py` 大負け対策の比較
- `study.py` 最初の版の間隔などの総当たり (4,968 通り → `results/grid.csv`)、`compare.py` 比較表と資産曲線 (`results/compare.md`, `results/equity.png`)

## 再現

```
pip install -r requirements.txt
python3 fetch_data.py      # data/ にダウンロード (コミットしない)
python3 -m pytest -q tests
python3 study.py && python3 compare.py
python3 study_cci.py && python3 study_risk.py
```

## 注意

- MetaEditor (MT4/MT5) でのコンパイルとストラテジーテスターでの確認はこの環境ではできていない。
- どの設定もはっきりした優位性は確認できていない (results/compare.md)。実口座の前にデモで確認すること。
