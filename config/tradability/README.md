# 取扱可否リスト(30.2.1)

このディレクトリには、証券会社ごとの米国株取扱銘柄一覧を1行1ティッカーの
プレーンテキストとして置く。ファイル名(拡張子を除く)がそのまま証券会社名として
APIレスポンス(`tradable_brokers`)に出る。

```
config/tradability/sbi.txt
config/tradability/rakuten.txt
config/tradability/ibkr.txt
```

```text
# 行頭 # はコメント。空行は無視。
# 2026-08-28 SBI証券 米国株取扱銘柄一覧より作成
AAPL
ABCD
```

**このディレクトリが空(またはファイルが1つも無い)状態は正常である。** その場合
すべての銘柄が `tradability: "unknown"` として返る(「取扱不可」とは断定しない)。
更新は手動でよい——取扱銘柄は日々変わるものではないので月1回程度で足りる。
このリストは秘密ではないので `.gitignore` に入れない。
