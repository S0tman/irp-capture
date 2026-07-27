# Sweden's pandemic decisions, 2020 to 2022

A worked example of the graph applied to a real, documented decision record.

**This is a reconstruction, not an IRP record.** These decisions were assembled
from public sources long after the fact. They were not captured through IRP, they
carry no attestation, and they are not evidence of anything beyond what their cited
sources say. That is why the export is built with `--reconstruction`, which removes
the attested-record wording and adds a standing notice, and why the ids use an
`SE-` namespace rather than `IRP-`: a reconstruction should not be capable of being
mistaken for a ledger entry, even out of context.

## What it is for

The graph normally shows a team its own reasoning. Here it is pointed at a record
everyone already has an opinion about, which makes the structure legible in a way
invented sample data never can.

Two things it deliberately does **not** do:

- It does not argue that Sweden was right, or wrong. The Corona Commission's final
  verdict was two-sided, that relying on voluntary measures rather than lockdown was
  "fundamentally correct" while the measures were "too weak and too late", and both
  halves are in the record.
- It does not resolve disagreement. Where sources conflict, the conflict is the
  content. Whether the constitution genuinely barred a lockdown or whether that was
  a justification after the fact is disputed, and the graph shows the dispute.

## Sourcing standard

Every node carries at least one citable source and an honest confidence. Nodes are
typed: `decision`, `constraint`, `dissent`, `rebuttal`, `reversal`, `assessment`,
`outcome`, `context`. Seven carry a `contested` note explaining how the same fact is
read in opposite directions.

`decisions.json` is the researched source, with sources and verification notes per
node. `ledger.jsonl` is generated from it in the shape the exporter reads.

## Regenerate

```
mkdir -p /tmp/se/.irp && cp ledger.jsonl /tmp/se/.irp/ledger.jsonl
cd /tmp/se && irp export graph --force --view foundations \
  --reconstruction --title "Sweden's pandemic decisions, 2020 to 2022" \
  --output sweden-2020.html
```
