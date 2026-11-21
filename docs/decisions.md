# Decisions

- Technical spec v3 governs the RAH 0.1.0 refactor. OpenRouter is the managed route for all demonstrated compatible model roles. Chat and embeddings have separate defaults. Users can select upstream-native configuration for standalone use.
- The initial managed PaperQA2 profile uses one verified onward provider per role, OpenRouter as the common gateway and local text-only PDF parsing. Every run requires its own confirmation. Zero Data Retention is an optional OpenRouter account setting linked from the confirmation.
- GPT Researcher's Classic browser UI ships through the same pin and environment as its CLI. One guarded loopback listener serves the native browser stack.
- One core install action covers LDR, GPT Researcher and PaperQA2. Final-path environments allow installs and native data to outlive the hub.
- Windows 11 x64, Apple silicon macOS and Ubuntu x64 are the desktop targets. The Windows Server CI runner covers installer and native startup behaviour. The pinned FAISS wheel establishes macOS 14 as the minimum candidate version.
- RAH-managed local models are deferred to consideration in 0.4.0 or later.
