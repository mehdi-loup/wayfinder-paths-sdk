// Run the SQL migration against Supabase.
// Usage: pnpm setup
// Prerequisite: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env
import 'dotenv/config'
import { readFileSync } from 'fs'
import { createClient } from '@supabase/supabase-js'
import { fileURLToPath } from 'url'
import { dirname, join } from 'path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const migration = process.argv[2] ?? '001_init'
const sql = readFileSync(join(__dirname, `../migrations/${migration}.sql`), 'utf8')

const url = process.env.SUPABASE_URL!
const key = process.env.SUPABASE_SERVICE_ROLE_KEY!

if (!url || !key) {
  console.error('Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env')
  process.exit(1)
}

const client = createClient(url, key)

// Supabase JS client doesn't expose raw SQL execution directly.
// Use the REST endpoint for DDL: POST /rest/v1/rpc/... won't work for CREATE TABLE.
// Instead, print instructions to run the migration in the Supabase SQL editor.
console.log(`
Migration: ${migration}.sql

Run the following SQL in your Supabase project's SQL editor:
  https://supabase.com/dashboard/project/_/sql

--- paste start ---
${sql}
--- paste end ---

Or use the Supabase CLI:
  supabase db push  (if you have a linked project)
  supabase migration up
`)
