"""Migrations idempotentes de la phase 3 : contexte de messagerie."""


def appliquer_schema_phase3(cur):
    cur.execute("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS contexte_type VARCHAR(20)")
    cur.execute("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS contexte_id INTEGER")
    cur.execute("ALTER TABLE materiel_maintenances ADD COLUMN IF NOT EXISTS conversation_id INTEGER")
    cur.execute("""DO $$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_constraint
                     WHERE conname='materiel_maintenances_conversation_id_fkey') THEN
        ALTER TABLE materiel_maintenances
          ADD CONSTRAINT materiel_maintenances_conversation_id_fkey
          FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE SET NULL;
      END IF;
    END $$""")
    cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_maintenance_conversation
                   ON materiel_maintenances(conversation_id)
                   WHERE conversation_id IS NOT NULL""")
    cur.execute("""CREATE INDEX IF NOT EXISTS idx_conversations_contexte
                   ON conversations(contexte_type,contexte_id)""")
    cur.execute("""DO $$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_constraint
                     WHERE conname='ck_conversations_contexte'
                       AND conrelid='conversations'::regclass) THEN
        ALTER TABLE conversations ADD CONSTRAINT ck_conversations_contexte
          CHECK (contexte_type IS NULL OR contexte_type IN ('maintenance','materiel')) NOT VALID;
      END IF;
    END $$""")
