import { describe, expect, it } from "vitest";
import viteConfig from "../vite.config";

/**
 * Defect 2(2026-09-05監査、docs/audit_followup_2026-09-05.md): Viteの既定
 * 挙動は「5173が埋まっていたら黙って5174へ」——APIのCORS許可リスト
 * (src/autoscreener/api/main.py)は`localhost:5173`/`127.0.0.1:5173`のみを
 * 許可するハードコードなので、黙って5174へ移ると全リクエストがCORSで
 * 失敗し、画面には汎用の接続エラーしか出ない。2026-09-05時点でこのマシン上
 * に5173と5174が同時にLISTENしていた、実際に踏んだ事故。
 *
 * `server.port`固定 + `strictPort: true`で、ポート衝突時は黙って別ポートへ
 * 移る代わりに起動自体を失敗させる。Python側の一致は
 * tests/unit/test_cors_vite_port_alignment.py が別途担保する(TS/Python
 * 境界をまたぐため、こちらは vite.config.ts 側の設定値のみを固定する)。
 */
describe("vite dev server port pinning", () => {
  it("pins the dev server to port 5173 (the port the API's CORS allowlist accepts)", () => {
    expect(viteConfig.server?.port).toBe(5173);
  });

  it("enables strictPort so a taken port fails loudly instead of silently drifting", () => {
    expect(viteConfig.server?.strictPort).toBe(true);
  });
});
