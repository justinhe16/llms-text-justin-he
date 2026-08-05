-- Local dev seed data.
--
-- Runs during `supabase db reset --local`, immediately after supabase/migrations/ (which
-- is deliberately empty — Prisma owns application migrations, see ARCHITECTURE.md §6) and
-- BEFORE `prisma migrate deploy` replays db/migrations/ (see the `reset` target in the
-- root Makefile). Consequence: this file may only depend on Supabase-managed schemas
-- (auth, storage) — application tables do not exist yet when it runs.
--
-- Re-running this file is safe: every statement is written to be a no-op the second time.

-- One fixed-UUID test user for local sign-in.
--   email:    dev@llms-text.test
--   password: devpassword123        (local dev only — this value is not a secret; the
--                                     database it unlocks is a container on localhost)
-- Local auth is email/password. GitHub OAuth is configured and verified in production
-- only (see README.md "Run locally").
insert into auth.users (
  instance_id,
  id,
  aud,
  role,
  email,
  encrypted_password,
  email_confirmed_at,
  raw_app_meta_data,
  raw_user_meta_data,
  created_at,
  updated_at,
  confirmation_token,
  recovery_token,
  email_change_token_new,
  email_change
)
values (
  '00000000-0000-0000-0000-000000000000',
  '11111111-1111-1111-1111-111111111111',
  'authenticated',
  'authenticated',
  'dev@llms-text.test',
  crypt('devpassword123', gen_salt('bf')),
  now(),
  '{"provider": "email", "providers": ["email"]}',
  '{}',
  now(),
  now(),
  '',
  '',
  '',
  ''
)
on conflict (id) do nothing;

-- An identity row is required in addition to the auth.users row, or password sign-in
-- fails with "Invalid login credentials" even though the user row exists.
insert into auth.identities (
  id,
  user_id,
  provider_id,
  identity_data,
  provider,
  last_sign_in_at,
  created_at,
  updated_at
)
values (
  '11111111-1111-1111-1111-111111111111',
  '11111111-1111-1111-1111-111111111111',
  '11111111-1111-1111-1111-111111111111',
  '{"sub": "11111111-1111-1111-1111-111111111111", "email": "dev@llms-text.test", "email_verified": true}',
  'email',
  now(),
  now(),
  now()
)
on conflict (provider_id, provider) do nothing;
