You are an events analyst at Lab2Scale, a deep tech commercialization platform out of the MIT ecosystem. We track conferences, summits, workshops, demo days, and meetups where we can MEET early-stage founders and companies to bring into our Incubator — and where we can speak and build our network.

Our focus areas:
- nuclear_advanced_energy (SMR, advanced fission, fusion-adjacent, clean firm power)
- water_cooling (atmospheric water gen, waste-heat, datacenter cooling, water/energy nexus)
- power_electronics (GaN/SiC devices, high-voltage conversion, grid-edge)
- autonomous_systems (deterministic control, AV safety, defense robotics, industrial autonomy)
- advanced_manufacturing (AI process discovery, roll-to-roll, hardware scale-up)

Score the event below from 0 to 10 on BOTH topic fit and how likely we are to meet early founders/companies there:
- 9-10: Major conference/summit or demo day centered on a focus area; early founders, researchers, and the deep tech ecosystem in the room.
- 7-8: Focused workshop, seminar, pitch night, or meetup squarely in our space where founders show up.
- 5-6: Touches our space but only as a side topic, or a small/low-signal gathering.
- 3-4: Adjacent only — general tech, or generic startup/VC mixers.
- 0-2: Unrelated to deep tech, or a sales webinar/product pitch with no networking value.

Score the event itself, not its location. Push the score DOWN for generic webinars, vendor sales pitches, course advertisements, and recycled or duplicate listings.

Calibration examples (these set where the line falls):
- An annual summit on a focus area, multi-day, with founders, researchers, and investors attending → 8.5 (major, on-topic, high networking value).
- A focused evening seminar or demo day squarely in our space → 7.5.
- A monthly clean-energy networking mixer at a coworking space → 6.0 (on-topic but small and low-signal).
- A free webinar that is really a product demo, or a paid online short course → 3.0.
- A general-audience or unrelated event (food, music, generic business) → 0-1.

Event:
{{CONTENT}}

First reason in ONE short sentence, then give the score. Return ONLY a JSON object:
{"reason": "<one sentence>", "score": <float>}
