# decisions.md — VideoUse (lokal)

**Konteks:** VideoUse adalah fork milik user (`wigu8989/video-use`) dari project open-source **video-use** — edit video lewat Claude Code: taruh footage mentah di folder, chat, dapat `final.mp4` (potong filler words, dead space, dll.).

## Tech stack (untuk orientasi)
- Python package (`pyproject.toml`), integrasi via skills Claude Code (`SKILL.md`, folder `skills/`, `helpers/`)
- Project video user di `videouse-projects/`
- Konfigurasi API key di `.env` (contoh di `.env.example`) — jangan commit `.env`

## Keputusan lokal
- Kandidat tool produksi untuk campaign clipping Content Rewards (edit klip 9:16 + subtitle) — belum dipakai untuk itu, baru kandidat
- Remote bernama `fork` (bukan `origin`) → push hanya ke fork sendiri, jangan ke upstream

_Dibuat 2026-07-11 sebagai starter; lengkapi saat sesi kerja berikutnya._
