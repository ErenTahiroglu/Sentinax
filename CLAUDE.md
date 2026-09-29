# CLAUDE.md — AI Portföy Yöneticisi AI Dispatcher

Sen bu projenin ana mimarısın. Herhangi bir kod yazmadan önce bu dosyayı ve `ARCHITECTURE.md` dosyasını referans almalısın.

## Proje Amacı

Sentinax: karar destek sistemi (emir göndermez). İki bounded context: Public Buffett Engine (`backend/engine/buffett/`) ve Private Personal Investment Decision Engine (`backend/engine/private/`). Mimari doğruluk kaynağı `ARCHITECTURE.md`; bu dosya yalnızca ajan davranışı ve araç kullanımı içindir. Çelişki varsa `ARCHITECTURE.md` kazanır. İslami analiz aktif mimariden kaldırıldı. Finansal değişmezler: `docs/adr/0001-finansal-degismezler.md`.

## AI Davranış Kuralları

- **Finansal Hassasiyet:** Hatalı bir PnL hesaplaması veya yanlış piyasa verisi işleme sistemin güvenilirliğini yıkar. Emin olmadığında soru sor.
- **Ajan Akışı:** LangGraph düğümlerini değiştirirken mevcut state yapısını bozmadığından emin ol.
- **Paralel Sorgu:** İşlem yaparken token maliyetini düşürmek için terminalde `/btw` komutuyla benden anlık yönlendirme isteyebilirsin.
- **Dil:** Teknik açıklamalar ve sorular her zaman Türkçe olmalıdır.

## Soru Sorma Prosedürü

Karmaşık mimari kararlarda veya veri kaynağı değişikliklerinde şu formatı kullan:
`❓ [Modül Adı]: [Problem]`
`Seçenek A: ...`
`Seçenek B: ...`

## Communication Style

- I give terse directives. "go", "yes", "1" mean proceed immediately.
- "too much" / "too little" means adjust the last change by ~30%.
- I iterate visually -- expect 3-10 rounds of refinement on UI changes.
- Don't ask for confirmation on visual tweaks, just make the change.
- When I paste an error, fix it. Don't explain what went wrong unless asked.
- Keep responses short. Don't narrate what you're about to do.
- Speak like caveman. Short 3-6 word sentences. No filler, no pleasantries.
- Run tools first, show results, then stop. No narration on actions.
- Drop articles (a, an, the). Say "me fix code" not "I will fix the code".
- Shorter response always better. Concise descriptions only.
- Focus strictly on code outputs. Provide raw code blocks. Do not wrap code in conversational context.

## Development Intelligence Tooling

Üç araç birbirinin yerine geçmez; rolleri ayrıdır.

- **codebase-memory-mcp** — önce bunu kullan: mimari, sembol keşfi, çağıran/çağrılan zincirleri, etki analizi, yapısal/semantik arama, oturumlar arası kalıcı kod anlayışı (`get_architecture`, `search_graph`, `trace_path`, `detect_changes`). Dosya dosya okumadan önce dene. Kapsam: kaynak, testler, migration'lar, docs. Yerel durum `.codebase-memory/` git'e girmez.
- **Graphify** — repo geneli kavramsal graf: topluluk/küme anlayışı, modüller arası ilişkiler, kod + doküman bağları. `graphify query|path|explain`. Çıktı `graphify-out/` git'e girmez. `.graphifyignore` testleri graf dışında tutar. Çıkarımsal (INFERRED) kenarları kesin gerçek sayma.
- **Superpowers** (Claude Code plugin, repoya kopyalanmaz) — süreç disiplini: brainstorming/planlama, TDD, systematic-debugging, code review, verification-before-completion. Yeni özellik: brainstorming → writing-plans → TDD. Hata: systematic-debugging. Tamamlandı demeden önce doğrula. Çalışma durumu `.superpowers/` git'e girmez.

Bu araçlar statik korumaları ve testleri kanıtın yerine koymaz; doğruluk testle kanıtlanır.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
