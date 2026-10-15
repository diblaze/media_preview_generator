# Final lab run (2026-10-15, image `nodes-2f0195e2`)

`lab/nodes_lab.sh up`: the app (`/media`), an NVIDIA worker node and a CPU worker node (`/mnt/nas`, same real files,
read-only), and a Plex database node beside `mlab-plex`'s config. The app's settings were a copy of a schema-19 lab
config, so the v24 migration ran on real settings. Every scenario passed; the bugs it found are fixed in later commits
(Test access chips squeezing the path fields, the turned-off-node wording, node worker names in the job log).

| File | Scenario |
| --- | --- |
| `s1_settings_after.txt` | Schema 19 → 24: the old helper becomes a legacy Plex database node; legacy `url`/`token` kept for rollback |
| `s2_*` | `/api/nodes*` refuses anonymous calls; a probe with a stored node's id, `****` and another URL is refused and the other URL receives nothing |
| `s4_logs.txt`, `s4_workers.txt`, `s4_bifs_written.txt` | Previews on node workers only: each file names its node, live percent/speed, BIFs written by the app |
| `s5m_logs.txt`, `s5l3_logs.txt`, `s5_node_plexdb.log` | Intro & Credits and loudness on node workers, written to Plex through the Plex database node |
| `s6a_logs.txt` | GPU node restarted 4 s into a 6-file job: 0 failed; its file went back once and finished |
| `s6b_*` | GPU node stopped for good mid-job: 0 failed, the rest finished on the CPU node; Offline + bell; Ready again after `docker start` |
| `shot_*.png` | Nodes page, node Setup Health, worker groups with node devices, dashboard during a node job, job log, Plex database selector |

Timings: GPU node 8.9–14.4 s per 4K HEVC episode; CPU node 166–178 s per 4K HEVC, 17–22 s per 1080p x264.
