# YM2612 DAC: release-capped PCM chunks, pan-aware

Status: **implemented**. The default is a sequence of **rate-exact PCM
chunks**, one FM6 note each. Chunks split at every `0xB6` pan change and at
most every 0.25 s, and the track is capped at 240 samples so Furnace 0.6.8.3
loads it. A chunk starts on an audio byte; silence-only phrases and leading
silence get no note. The per-run slice path is explicit
(`collect_dac(..., slices=True)`), not the default. The old 22.05 kHz body is
`collect_dac(..., timeline=True)` for comparison only.

This replaces the previous one-sample-per-audio-phrase default. That was one
matryoshka sample: one FM6 note held the first pan for the whole track (on
Contra 02 the note was `08 0F`, right only). The source writes 24 `0xB6`
values on track 02 alone (`0xE0/0xC0` both, `0x40` right, `0x80` left, then
`0x40/0x80` alternating). A single note cannot represent them. Chunks are the
smallest representation that is still a real sequence, keeps the source
bytes, and fits the release sample limit.

## Why chunks, not one phrase or thousands of slices

- **Release sample limit.** The 130-sample probe module loads in 0.6.8.3; a
  392-sample slice track fails with `invalid sample count!`; the old 1,098/
  3,054/12,624 slice sets fail even with every map base 0. The chunked
  75/219/240 modules load. Track 20 at 240 samples renders a
  57,995,896-byte WAV.
- **One phrase is a matryoshka and loses pan.** It has one note, one pan. The
  source's pan changes are lost; on 02 the first note was right for the
  entire track.
- **Thousands of slices do not load.** The slice path is kept for editing,
  but it cannot be the release default on a dense track.
- **A chunk sequence is real PCM.** Each chunk is source bytes at the
  natural rate; the pattern has one note per chunk, not one selector per
  silence and not one sample for the whole track.

## What the converter does

`vgm2151fur/ym2612dac.py`:

1. **Decode.** Old `0xE0`/`0x80`-`0x8F`, direct `0x2A` and modern `0x92`/
   `0x95` become one ordered word stream. `0x2B` bit 7 gates audibility.
   Several values at one VGM timestamp collapse to the final value (the
   reference player writes them at one output sample) with a warning.
2. **Phrases.** A phrase splits only on a pause larger than 2,048 VGM
   samples, so short inter-hit rests stay in the PCM.
3. **Chunks.** Within a phrase, audio words split at every `0xB6` pan change
   and into at most 0.25 s pieces. The total is capped at
   `DAC_RELEASE_SAMPLES = 240`; excess chunks are trimmed with a report
   stat rather than producing an unloadable module.
4. **Body.** Each chunk starts at an audio word, never on a rest. It stops at
   its last audio word plus one trailing `0x80` frame, so Furnace holds
   silence after the chunk. Intermediate silence bytes are the source PCM
   hold. Leading silence and silence-only phrases produce no sample/note.
5. **Rate.** `round(44100 / smallest positive write interval in the chunk)`
   makes every byte occupy a distinct frame; the minimum and maximum are
   clamped. Holds fill the gaps.
6. **Pan.** `DacPlay.pan` is `_pan_at(0xB6)` at the chunk start. The writer
   turns it into `08 xy` (`C0`→`FF` both, `80`→`F0` left, `40`→`0F` right).
7. **Place/emit.** `group_dac_plays` puts the chunk on the preceding tick,
   merges chunks that land in one row without dropping audio, and emits one
   note per surviving chunk. Samples are grouped 120 per `DIV_INS_AMIGA` map;
   every map reports `first_sample=0`.

The explicit `slices=True` path is unchanged except that a pure-silent row is
skipped and the preceding sample gets a zero tail. The old `timeline=True`
body is not used.

## Release evidence

`C:\Users\User\Music\furnace-0.6.8.3-win64\furnace.exe`:

- 130-sample probe (two maps, base 0): renders;
- 392-sample x4 slice: rejects;
- 75-sample chunk 02: renders, 5,509,576-byte WAV;
- 219-sample chunk 04: renders, 16,895,664-byte WAV;
- 240-sample chunk 20: renders, 57,995,896-byte WAV;
- the one-phrase 02/04 (64,028/297,651 bytes) also rendered, but its one note
  held the intro right pan for the whole track; it is superseded by chunks.

## Measured cost (compressed `.fur`, zlib 5)

| Track | Old 22.05k timeline | Silence-free slices | One phrase (wrong pan) | Chunks |
| --- | ---: | ---: | ---: | ---: |
| 02 Locked & Loaded | 91,052 | 41,221 | 64,028 | **66,768** |
| 04 Contra Overdrive | 367,285 | 239,146 | 297,651 | **305,234** |
| 20 Moonlit Army | 1,289,422 | 724,017 | 1,013,233 | **1,021,470** |

| Track | Chunk samples/notes | Sample maps | Pan chunks | Pan distribution (notes) |
| --- | ---: | ---: | ---: | --- |
| 02 | 75 | 1 | 24 | 57 both, 12 right, 6 left |
| 04 | 219 | 2 | 3 | 196 both, 23 right |
| 20 | 240 | 2 | 3 | 234 both, 6 right |

Track 02's first 12 right notes are the source's intro section: `0xB6=0x40`
is written at sample 195 before the first audio word at 369. The old
`fur-fixed` one-note timeline was `08 FF` (both) because its note started at
sample 0, before that write; the chunked default follows the source. The rest
of track 02 is 57 both and 6 left, so the track is no longer right-only.

The x4/02 module is 66,510 bytes, 75 samples, one map, 75 notes, same pan
distribution. The 04 default/x4 is 219 samples/two maps; 20 is 240/two maps.
The 02 chunk count fits one 120-slot map; that is a map-capacity fact, not a
single note.

Full pack after the chunked change: 203 modules, 68,388,008 bytes. The 1x
`condense.tsv` reports 50,692 keys (45,177 FM + the DAC chunk notes), 0
missed, `psg_miss=0`, 13 field differences and up to 2,182 ms divergence from
the newly audible PSG path. The old 45,500/0 figure was measured with PSG
notes pointing at a DAC map, i.e. silent.

## Pan and the right-channel report

The previous one-phrase default put one note at the first audio phrase and
therefore one `08` effect for the whole track. On 02 that was `08 0F` (right),
matching a `0xB6=0x40` write at the phrase start and losing all later pan. The
chunked default emits an effect on each chunk. The first 12 notes are still
right because the source is right there; after the `0xC0` write at 151,224
the notes are both, and after `0x80` they are left. If the source pan is not
wanted, that is a source/authoring decision, not a converter stuck on right.

## Silence rule

- A phrase whose signed bytes are all `0x00` (source `0x80`) gets no chunk.
- A chunk starts on an audio word; leading silence is not stored.
- One trailing zero frame sets the DAC hold to silence; the rest is free.
- A short silence between audio hits inside a chunk is the source's PCM hold.
  It is not a separate note.
- The slice mode drops pure-silent rows and appends a zero tail to the
  preceding sample.

## Tests

`tests/test_vgm2151fur_ym2612.py` and the new-chip volume tests (384 passed,
1 skipped):

- chunked: 20 s of PCM becomes >1 and <=240 chunks at the 0.25 s budget;
  three source pans appear on chunk notes and module `08` effects; a short
  300-byte stream stays one chunk; a long gap splits audio and drops the
  silence phrase; a silence-only source has no notes; the smallest interval
  sets the rate; a map's base is 0;
- explicit slice: direct/block plays, long gaps, holds, same-row merge, maps,
  pan, disabled DAC, export round trip, silence-row removal;
- PSG binding to INS_STD after a DAC and pitch copy;
- the full suite and Furnace round trips.

## Remaining limits

- The 240-sample cap is the release wall. A track with very many pan changes
  can trim chunks; `dac_release_trimmed` reports it.
- The x4 FM condense still drops/merges FM rows by design.
- The newly audible PSG exposes PSG pitch differences; the FM-only TFIV audit
  remains clean. Split the audit before quoting an all-channel zero.
- A chunk may contain short internal silence; it is PCM, not a rest note.
- The 02 map count is one because 75 <= 120; do not read that as one note.

## Do not do this

- Do not collapse a track back to one sample/note; the pan and the sequence
  both disappear.
- Do not emit more than 240 samples for a dense release module.
- Do not put a note or selector on a silence-only row/phrase.
- Do not use a nonzero `first_sample`; 0.6.8.3 reads it as an embedded sample
  count.
- Do not point PSG/SCC/AY notes at a DAC map; count the DAC maps once.
- Do not ignore `0x2B`.

## Related files

- `../vgm2151fur/ym2612dac.py` — decoder, chunk builder, slice mode, silence.
- `../vgm2151fur/analyze.py` — DAC/tone bases, PSG binding, report.
- `../vgm2151fur/furwrite.py` — sample maps, base 0, pan effects.
- `../vgm2151fur/audit.py` — source/export key-on, DAC start/rate audit.
- `../tests/test_vgm2151fur_ym2612.py` — chunk/slice/silence/PSG tests.
- `ym2612.md` — registers and verification.
- `implementation-notes.md` — evidence loop.
