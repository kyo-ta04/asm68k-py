# NOTICE — EhBASIC sources (third party)

このフォルダのファイルは、このリポジトリのソフトウェアではありません。
**このリポジトリの MIT ライセンスの対象外** です。

## 出典

- EhBASIC (Enhanced 68k BASIC) Version 3.54, Copyright (C) 2002-2012 Lee Davison
- 入手元: https://github.com/satoshiokue/EhBASIC68k-EMU
- 以下の3ファイルは上記リポジトリから **無改変** で収録しています
  （asm68k の検証を再現可能にするため）:

| ファイル | SHA-256 |
|---|---|
| basic68k.asm | a069f993d561ccf37081641c154e09f9618debd9f5826404338e81cda2f3be43 |
| basic68k.inc | 42cbab431ee387e3769b6d47324324a06889578dcf7a1d6ef0ca6443b4f8cd62 |
| EhBASICv354.txt | 4b0a1053b055b3f36073458067cb29ea34c91faf475e0fee1fa8c53c9e148efc |

## ライセンス条件（原文のまま）

ソース先頭の表記:

> Copyright(C) 2002-12 by Lee Davison. This program may be freely distributed
> for personal use only. All commercial rights are reserved.

入手元 README の表記:

> EhBASIC is copyright Lee Davison 2002 - 2012 and free for educational or
> personal use only.
> For commercial use please contact me at leeedavison@lgooglemail.com for conditions.

つまり、教育・個人利用の範囲では自由に配布できますが、商用権利は作者に
留保されています。このフォルダのファイルを使う場合は、上記の条件に
従ってください。

## 検証の再現方法

素の設定（リポジトリのルートで実行）:

```
python asm68k.py third_party/ehbasic/basic68k.asm --name EhBASIC -o _ehbasic.c --check third_party/ehbasic/EhBASICv354.txt
```

`check OK: identical (16384 bytes)` と出れば、リファレンスと完全一致です。

EMUC68000-PGA2350B 向け設定の再現には、上記ファイルのコピーに対して
以下の3点だけを変更し、ファームウェア
(https://github.com/kyo-ta04/EMUC68000-PGA2350B) の ehbasic.c と照合します
（変更済みソース自体は同梱していません）:

- basic68k.inc: `RAM_SIZE $02000` → `$40000`
- basic68k.asm: `ACIAD $0000E000` → `$0080E000`、`ACIAC $0000E001` → `$0080E001`

```
python asm68k.py <変更したフォルダ>\basic68k.asm --name EhBASIC --check <ehbasic.c のパス> --array EhBASIC
```
