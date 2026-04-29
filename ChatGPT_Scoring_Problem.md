I am designing a scoring mechanism for a fuzzy image search algorithm.
The problem:
- domain: a set of videos each with a cover image and a sprite sheet of 81 snapshots taken at even intervals during the video runtime
- codomain: a set of metadata with 0..N images that may correspond to any or no frames in the input video set.
- function: hash the domain and codomain and perform hamming distance calculations between each member of the codomain image set and the domain image set. Take the inverse square as the score: 1 - d / hash_size². Group the scores for each domain member by codomain image to form a [0,1] similarity distribution across 0..N record images. Then calculate a weighted average of similarity between the codomain's metadata record images similarity and each domain video sprite sheet/cover image.

The last part is where the bottom falls out. Some records may contain 1 image, some 100. The domain sprite count is steady at 81 images. I need a method that accurately estimates the probability of the domain to codomain similarity given the following conditions:
- domain video duration: (1 -> ∞); inverse representational fidelity of 81 image sprite sheet to video duration
- number of codomain images: (0 -> ∞); the greater the number, the more chances to match sprites but inversely, the lower the number, the more chance that a single match may misrepresent the overall similarity between the domain video timeline and codomain image(s).
- signal strength bias: (0 -> 1), some images, and therefore matches, may not represent semantic similarity due to less information available in the frame.  A blank wall for instance, or video credits.
- signal distribution bias: [0,1] ∈ {0..N: codomain images}.  In a set of 4 codomain images, is a an aggregate score of 1.3 distributed as [1.,.1,.1,.1] the same as another aggregate score of 1.3 distributed as [0.13,0.13,0.13,0.13]?

Your “bottom falls out” because a **weighted average is the wrong final operator**. You need an evidence model with **coverage, confidence, and saturation**.

A good structure:

P(\text{match}) = 1 - \prod_{i=1}^{N} \left(1 - q_i \cdot c_i \cdot m_i\right)

Where for each codomain image `i`:

`m_i` = best similarity against the video sprites/cover
`q_i` = signal quality of codomain image
`c_i` = coverage/uniqueness weight
`N` = number of codomain images

This gives you an **evidence union**, not an average. A single strong match helps, but 100 weak/redundant images do not linearly inflate the score.

For each codomain image:

```text
m_i = max_j similarity(codomain_image_i, domain_frame_j)
```

But do **not** use the raw max directly. Use a sharpened version:

```text
m_i' = max(0, (m_i - baseline) / (1 - baseline))^γ
```

Example:

```text
baseline = expected random similarity
γ = 2 or 3
```

This suppresses accidental fuzzy matches.

Then estimate signal strength:

```text
q_i ∈ [0,1]
```

Based on image entropy, edge density, color variance, perceptual-hash rarity, face/object/text presence, etc. A blank wall, black frame, credits screen, or common title card should get low `q_i`.

For video duration, discount sparse temporal representation:

```text
duration_penalty = sqrt(81 / effective_video_seconds)
```

clamped to something like `[0.25, 1]`.

Longer videos have lower sprite fidelity, so a match against one of 81 frames should be slightly less conclusive than the same match in a short clip.

For codomain count, use saturation rather than averaging:

```text
effective_N = sum(q_i * uniqueness_i)
count_confidence = 1 - exp(-effective_N / k)
```

Then final score:

```text
final_score = evidence_score * count_confidence * duration_penalty
```

The key distinction:

```text
[1.0, 0.1, 0.1, 0.1]
```

is **not** the same as

```text
[0.325, 0.325, 0.325, 0.325]
```

even though both sum to `1.3`.

The first says:

> one image strongly matches, the others barely do.

The second says:

> all images weakly/moderately support the same video.

So include a distribution-shape term, such as entropy or concentration:

```text
concentration = max(m_i') / sum(m_i')
coverage = entropy(normalized m_i')
```

Then interpret:

```text
high max + low coverage  => possible single-frame coincidence
medium max + high coverage => broader semantic/timeline similarity
high max + high coverage => strong candidate
```

A practical final formula:

```text
score =
  evidence_union
  * count_confidence
  * duration_penalty
  * distribution_quality
```

Where:

```text
evidence_union = 1 - Π_i(1 - q_i * uniqueness_i * m_i')
```

and:

```text
distribution_quality = 0.5 + 0.5 * normalized_entropy(m_i')
```

That way, a single perfect match is still useful, but a distributed set of matches is more trusted.
