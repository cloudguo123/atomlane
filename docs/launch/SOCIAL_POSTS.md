# Short launch posts

## X / LinkedIn

I built AtomLane: parallelize only what is proven safe. It makes local builds, tests, Docker, and research pipelines faster without guessing away task semantics.

Typed atomic plans · fail-closed effects · host-aware resources · Windows Job containment · live progress · per-run + cumulative savings.

Controlled 5+ min run: 20m41s serial equivalent → 5m10s wall, 15m31s saved, 4.00× observed. Not a universal claim; method and JSON are public.

https://github.com/cloudguo123/atomlane

## Reddit

I made a local Codex plugin for the part of parallel execution that usually worries me: preserving semantics. macOS is stable for the supported shell/package/Make/Compose/test frontends; native Windows now has a scoped Preview for exact argv and declared PowerShell files, with Job Object containment and separate WSL/Docker realms. It compiles work into a typed plan, keeps success/failure/data/lifecycle edges distinct, models shared artifacts and capacity, and refuses unknown effects. Long runs stream real-time progress and savings.

The public controlled benchmark runs four independent tasks for 5+ minutes and reports 20m41s serial equivalent vs 5m10s wall (4.00× observed). The method and raw result are linked, and real-project neutral/negative results are welcome.

Source: https://github.com/cloudguo123/atomlane

## Video description

AtomLane is a free, MIT-licensed Codex plugin for safe local concurrency on macOS stable and a scoped native Windows Preview. This short demo shows the live execution view, including elapsed time, running/ready/completed/failed counts, and current estimated savings. The linked report contains reproducible regression evidence and controlled five-minute benchmarks.

Install and source: https://github.com/cloudguo123/atomlane

Evidence: https://cloudguo123.github.io/atomlane/
