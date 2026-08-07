# Floors

The journal tags every entry with a Floor: the emotional consciousness level you were on when you wrote it. Over time the tag becomes infrastructure. Patterns surface, loops get named, and you stop being surprised by your own weather.

## Where the floors come from

The 34-floor model is the open-source **High-Rise framework**, owned and published by [Fundación Lontananza](https://github.com/Fundacion-Lontananza/high-rise) under MIT. ai-brain-starter **consumes** the framework as a downstream dependency. It does not keep its own copy of the list.

The canonical reference lives vendored and pinned at **[`vendor/high-rise/floors.md`](vendor/high-rise/floors.md)**: the 34-floor table with energies and Spanish names, the three tiers, the elevator emotions, and the shadow twins, in English and Spanish. That is the pinned source this repo's floor tooling reads. For example, `scripts/generate_floor_stubs.py` regenerates the per-floor notes under [`floors/`](floors/) from it, so a wikilink like `[[Fear]]` in a journal entry resolves to a real node with body content, elevator edges, and a shadow-twin link.

To change the floors, change them upstream in `Fundacion-Lontananza/high-rise`, cut a release, then run `scripts/sync-high-rise.py --tag vX.Y.Z` and re-run `scripts/generate_floor_stubs.py`. Nothing in this repo hand-maintains a second floor list. See [`vendor/high-rise/README.md`](vendor/high-rise/README.md).

**If you just want the list, open [`vendor/high-rise/floors.md`](vendor/high-rise/floors.md).**

## How the journal uses the floor

The `daily-journal` skill asks one question at the end of each entry: where were you today? The answer becomes a frontmatter field on the saved entry:

```yaml
floor: Hope
floor_level: Middle
```

That tag is the seed. Over weeks, it tells you which floors you spend most time on, which ones you cycle through, which loops you are stuck in. The `patterns`, `insights`, and `coaching` skills read it. The `rise` morning skill reads yesterday's tag to choose today's body movement.

You can backfill past entries by running the journal skill with a date range.

## How to use the framework if you do not journal

You do not need the install for the floors to be useful. Three reps that work on their own:

1. **Name the floor before reacting.** When you feel something hard, the first move is to name which floor it is. The naming itself reduces the charge. You are on Frustration, not in Frustration.
2. **Check for shadow twins.** When a high floor shows up easily, ask whether the shadow is in costume. Most "Acceptance" is Resignation. Most "Confidence" is Pride. The difference is whether you are still bracing.
3. **Watch elevators, not just floors.** What carried you from one floor to another? A walk. A conversation. A meal. A meeting. The elevators are the actual lesson, and the floors are where the elevator opens.

## The narrative form

These floors started as a writing project, where each one gets a chapter of lived experience instead of a one-line definition. If you want the floors as stories rather than as a table, a companion read lives at [adelaidadiazroa.substack.com](https://adelaidadiazroa.substack.com) (English) and [perspectivasblog.substack.com](https://perspectivasblog.substack.com) (Spanish).

License: MIT. Use the framework. Translate it. Teach it. Ship it. The point is people moving through their own buildings.

---

## Pisos (Español)

El journal etiqueta cada entrada con un Piso: el nivel de consciencia emocional en el que estabas cuando escribiste. Con el tiempo la etiqueta se vuelve infraestructura. Aparecen patrones, los loops reciben nombre, y dejás de sorprenderte con tu propio clima.

El modelo de 34 pisos es el marco **High-Rise** de código abierto, propiedad de [Fundación Lontananza](https://github.com/Fundacion-Lontananza/high-rise) bajo licencia MIT. ai-brain-starter lo **consume**, no guarda su propia copia. La referencia canónica (la tabla de pisos con energías y nombres en español, los tres tiers, las emociones-ascensor y los gemelos sombra) vive fijada en [`vendor/high-rise/floors.md`](vendor/high-rise/floors.md). Para cambiar los pisos, se cambian aguas arriba en `Fundacion-Lontananza/high-rise` y se corre `scripts/sync-high-rise.py`.

### Cómo lo usa el journal

La skill `daily-journal` hace una pregunta al final de cada entrada: ¿dónde estuviste hoy? La respuesta se guarda en el frontmatter (`floor:` + `floor_level:`). Esa etiqueta es la semilla. Con el tiempo te dice en qué pisos pasás más tiempo, cuáles ciclás, en qué loops estás. Las skills `patterns`, `insights` y `coaching` lo leen. La skill `rise` lee el piso de ayer para elegir el movimiento corporal de hoy.

### Si no usás el journal

No necesitás la instalación para que los pisos sean útiles. Tres ejercicios que funcionan solos:

1. **Nombrá el piso antes de reaccionar.** Cuando sentís algo duro, primero nombrá qué piso es. El nombrarlo ya reduce la carga. Estás en Frustración, no sos Frustración.
2. **Chequeá los gemelos sombra.** Cuando un piso alto aparece muy fácil, preguntá si la sombra está disfrazada. La mayoría de "Aceptación" es Resignación. La mayoría de "Confianza" es Orgullo. La diferencia es si todavía estás tensa.
3. **Mirá ascensores, no sólo pisos.** ¿Qué te llevó de un piso a otro? Una caminata. Una conversación. Una comida. Una reunión. Los ascensores son la verdadera lección, y los pisos son donde el ascensor abre la puerta.
