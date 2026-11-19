-- Tables for Chainlit's chat history (threads, messages, feedback).
-- Translated from the official Chainlit data-layer Prisma schema
-- (github.com/Chainlit/chainlit-datalayer, prisma/schema.prisma). Chainlit does not
-- create these itself. Postgres runs this file once, when the database volume is empty.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE "StepType" AS ENUM (
    'assistant_message', 'embedding', 'llm', 'retrieval', 'rerank',
    'run', 'system_message', 'tool', 'undefined', 'user_message'
);

CREATE TABLE "User" (
    "id"         TEXT PRIMARY KEY DEFAULT gen_random_uuid(),
    "createdAt"  TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt"  TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "metadata"   JSONB NOT NULL,
    "identifier" TEXT NOT NULL
);
CREATE UNIQUE INDEX "User_identifier_key" ON "User"("identifier");

CREATE TABLE "Thread" (
    "id"        TEXT PRIMARY KEY DEFAULT gen_random_uuid(),
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "deletedAt" TIMESTAMP(3),
    "name"      TEXT,
    "metadata"  JSONB NOT NULL,
    "tags"      TEXT[] DEFAULT ARRAY[]::TEXT[],
    "userId"    TEXT REFERENCES "User"("id") ON DELETE SET NULL ON UPDATE CASCADE
);
CREATE INDEX "Thread_createdAt_idx" ON "Thread"("createdAt");
CREATE INDEX "Thread_name_idx" ON "Thread"("name");
CREATE INDEX "Thread_userId_updatedAt_idx" ON "Thread"("userId", "updatedAt");

CREATE TABLE "Step" (
    "id"        TEXT PRIMARY KEY DEFAULT gen_random_uuid(),
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "parentId"  TEXT REFERENCES "Step"("id") ON DELETE CASCADE ON UPDATE CASCADE,
    "threadId"  TEXT REFERENCES "Thread"("id") ON DELETE CASCADE ON UPDATE CASCADE,
    "input"     TEXT,
    "metadata"  JSONB NOT NULL,
    "name"      TEXT,
    "output"    TEXT,
    "type"      "StepType" NOT NULL,
    "showInput" TEXT DEFAULT 'json',
    "isError"   BOOLEAN DEFAULT false,
    "startTime" TIMESTAMP(3) NOT NULL,
    "endTime"   TIMESTAMP(3) NOT NULL
);
CREATE INDEX "Step_createdAt_idx" ON "Step"("createdAt");
CREATE INDEX "Step_parentId_idx" ON "Step"("parentId");
CREATE INDEX "Step_threadId_startTime_endTime_idx" ON "Step"("threadId", "startTime", "endTime");
CREATE INDEX "Step_type_idx" ON "Step"("type");

CREATE TABLE "Element" (
    "id"          TEXT PRIMARY KEY DEFAULT gen_random_uuid(),
    "createdAt"   TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt"   TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "threadId"    TEXT REFERENCES "Thread"("id") ON DELETE CASCADE ON UPDATE CASCADE,
    "stepId"      TEXT NOT NULL REFERENCES "Step"("id") ON DELETE CASCADE ON UPDATE CASCADE,
    "metadata"    JSONB NOT NULL,
    "mime"        TEXT,
    "name"        TEXT NOT NULL,
    "objectKey"   TEXT,
    "url"         TEXT,
    "chainlitKey" TEXT,
    "display"     TEXT,
    "size"        TEXT,
    "language"    TEXT,
    "page"        INTEGER,
    "props"       JSONB
);
CREATE INDEX "Element_stepId_idx" ON "Element"("stepId");
CREATE INDEX "Element_threadId_idx" ON "Element"("threadId");

CREATE TABLE "Feedback" (
    "id"        TEXT PRIMARY KEY DEFAULT gen_random_uuid(),
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "stepId"    TEXT REFERENCES "Step"("id") ON DELETE SET NULL ON UPDATE CASCADE,
    "name"      TEXT NOT NULL,
    "value"     DOUBLE PRECISION NOT NULL,
    "comment"   TEXT
);
CREATE INDEX "Feedback_stepId_idx" ON "Feedback"("stepId");
CREATE INDEX "Feedback_name_value_idx" ON "Feedback"("name", "value");
