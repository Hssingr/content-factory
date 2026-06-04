import AISuggestionField from '../AISuggestionField'
import { TONES } from '../../constants'

export default function BasicInfoSection({ description, setDescription, name, setName, niche, setNiche, tone, setTone, ctx }) {
  const hasDescription = description.trim().length > 0

  return (
    <>
      {/* Description is always enabled; suggest is only available when empty */}
      <AISuggestionField
        label="Channel description"
        field="description"
        value={description}
        onChange={setDescription}
        context={ctx}
        placeholder="Describe what you want to create — topic, audience, style, goal…"
        multiline
        disableSuggest={hasDescription}
      />

      {/* All other fields unlock only when description is filled */}
      <AISuggestionField
        label="Channel name"
        field="name"
        value={name}
        onChange={setName}
        context={ctx}
        placeholder="e.g. Decoded History"
        disabled={!hasDescription}
      />
      <AISuggestionField
        label="Niche"
        field="niche"
        value={niche}
        onChange={setNiche}
        context={ctx}
        placeholder="e.g. cold war espionage"
        disabled={!hasDescription}
      />
      <AISuggestionField
        label="Tone"
        field="tone"
        value={tone}
        onChange={setTone}
        context={ctx}
        options={TONES}
        disabled={!hasDescription}
      />
    </>
  )
}
