-- 18.6: API層用の読み取り専用DBロールを作成する。
-- 新規環境セットアップ時、DBマイグレーション後に1回だけ実行する
-- (docker compose exec -T db psql -U autoscreener -d autoscreener < scripts/create_readonly_role.sql)。
--
-- 実行前に CHANGE_ME を実際のパスワードに置き換え、
-- .env の API_DATABASE_URL にも同じ値を設定すること。

DO $$
BEGIN
   IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'autoscreener_readonly') THEN
      CREATE ROLE autoscreener_readonly WITH LOGIN PASSWORD 'CHANGE_ME';
   END IF;
END
$$;

GRANT CONNECT ON DATABASE autoscreener TO autoscreener_readonly;
GRANT USAGE ON SCHEMA public TO autoscreener_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO autoscreener_readonly;
-- 将来追加されるテーブルにも自動的にSELECTのみ付与する
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO autoscreener_readonly;
