# Reusable architecture diagram prompt

Copy the prompt below, replace the bracketed inputs, and attach a diagram you want to use as a visual reference.

```text
Create a clean architecture diagram for a single PowerPoint slide. Match the attached reference's visual style, spacing, infrastructure boundaries, and connected request flow.

The diagram should stay on screen while a presenter explains the architecture. A viewer should quickly understand where a request enters, how it travels, where processing happens, and how the response returns.

INPUTS
- System name: [NAME]
- Audience: [AUDIENCE]
- Main request and response flow: [ORDERED COMPONENTS]
- Setup, routing, or orchestration flow: [COMPONENTS, IF APPLICABLE]
- Infrastructure boundaries: [CLOUD, REGIONS, NETWORKS, HOSTS, CONTAINERS]
- Processing and storage components: [SERVICES AND THEIR ROLES]
- Optional branches: [ALTERNATE SITES, WORKLOADS, OR TENANTS]
- Confirmed deployment status: [LIVE COMPONENTS AND ANY PLANNED COMPONENTS]
- Animation: [YES / NO]

COMPOSITION
Use one unified 16:9 canvas at 1920x1080.

Place the user or client on the left. Make the primary workload flow read left to right through networking, security, ingress, application services, and processing.

Use nested boundaries to show actual containment: cloud -> site -> host -> CPU/GPU services, where applicable. Clearly distinguish infrastructure boundaries from individual services.

If setup or placement is separate from workload traffic, place it across the upper portion. Connect it clearly to the selected destination. Expand the main destination and keep alternate destinations compact.

Show request and response directions accurately. Use solid blue arrows for workload traffic and dashed muted arrows for setup or control traffic. Dashed lines should have a clear meaning in the legend.

Route connectors cleanly around labels. Avoid unnecessary crossings. Show branches and gateway-to-worker calls accurately; do not force independent services into a false sequential pipeline.

VISUAL STYLE
Use the approved light Akamai style:
- White background.
- Navy headings: #002F6C.
- Dark body text: #0B1F3A.
- Blue connectors: #0A6CAD.
- Pale gray infrastructure areas: #F5F7FA.
- Pale blue GPU or processing areas: #EAF6FD.
- Thin borders: #B7C5D8.
- Orange active-flow highlight: #FF8B00.

Use Instrument Sans for labels and JetBrains Mono for step numbers. Use the supplied official Akamai logo without redrawing it.

Use simple, consistent outline icons, generous spacing, restrained corners, and readable presentation text. Give each component a short name and at most one brief supporting line.

Keep the title at the top and a small legend at the bottom. Keep detailed explanations in speaker notes. Avoid paragraphs, dense footnotes, decorative cards, gradients, and em dashes.

ACCURACY
Use only supplied components and confirmed relationships. Do not invent infrastructure, isolation guarantees, or performance measurements. Keep confirmed live components labeled accurately. List unresolved questions separately from the slide.

ANIMATION, IF REQUESTED
Keep the entire architecture visible and stationary. Move one orange marker along the actual connectors, highlighting the active component and path. Show a short step number and action label. Pause briefly at meaningful stages and loop smoothly.

Animation timing is presentation pacing; display measured latency only when supplied.

DELIVERABLES
Provide a high-resolution PNG, editable SVG, and one widescreen PowerPoint slide. If animated, also provide a looping GIF embedded directly in the PowerPoint, with no external media dependencies.

Inspect the final output for legibility, clipped labels, connector collisions, and unnecessary detail.
```
