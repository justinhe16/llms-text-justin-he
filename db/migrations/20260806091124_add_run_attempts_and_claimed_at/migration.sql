-- AlterTable
ALTER TABLE "runs" ADD COLUMN     "attempts" INTEGER NOT NULL DEFAULT 0,
ADD COLUMN     "claimed_at" TIMESTAMPTZ(6);
