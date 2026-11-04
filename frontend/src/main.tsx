import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource/ibm-plex-sans/latin-400.css'
import '@fontsource/ibm-plex-sans/latin-500.css'
import '@fontsource/ibm-plex-sans/latin-600.css'
import '@fontsource/ibm-plex-sans/latin-ext-400.css'
import '@fontsource/jetbrains-mono/latin-400.css'
import { post, request } from './api'
import type { Config, Format, Inspection, Job, Platform } from './api'
import './styles.css'

const names: Record<Platform | 'file', string> = {
  youtube: 'YouTube',
  instagram: 'Instagram',
  tiktok: 'TikTok',
  file: 'Local file',
}

function durationLabel(duration: number) {
  const seconds = Math.floor(duration)
  return Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0')
}

function MediaPreview({ file, audio }: { file: File; audio: boolean }) {
  const [url, setUrl] = useState('')
  useEffect(() => {
    const objectUrl = URL.createObjectURL(file)
    setUrl(objectUrl)
    return () => URL.revokeObjectURL(objectUrl)
  }, [file])
  if (!url) return null
  return audio
    ? <audio aria-label="Source audio preview" controls src={url} />
    : <video aria-label="Source video preview" controls playsInline preload="metadata" src={url} />
}

function App() {
  const [config, setConfig] = useState<Config>()
  const [mode, setMode] = useState<'link' | 'file'>('file')
  const [link, setLink] = useState('')
  const [authorized, setAuthorized] = useState(false)
  const [file, setFile] = useState<File>()
  const [source, setSource] = useState<Inspection>()
  const [format, setFormat] = useState<Format>('mp4')
  const [quality, setQuality] = useState('')
  const [job, setJob] = useState<Job>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const chosenMode = useRef(false)
  const active = job?.status === 'queued' || job?.status === 'running'
  const jobId = job?.id
  const options = format === 'mp4' ? source?.video : source?.audio

  useEffect(() => {
    const controller = new AbortController()
    request<Config>('/config', { signal: controller.signal }).then(value => {
      setConfig(value)
      if (!chosenMode.current && value.link_platforms.length) setMode('link')
    }).catch(() => {
      if (!controller.signal.aborted) setError('The server is unavailable. Refresh the page to reconnect.')
    })
    return () => controller.abort()
  }, [])

  useEffect(() => {
    if (!active || !jobId) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try {
        const next = await request<Job>('/jobs/' + jobId, { signal: controller.signal })
        if (controller.signal.aborted) return
        setJob(current => current?.id === next.id && ['queued', 'running'].includes(current.status) ? next : current)
        if (next.status === 'queued' || next.status === 'running') timer = setTimeout(poll, 900)
      } catch (failure) {
        if (controller.signal.aborted) return
        const message = failure instanceof Error ? failure.message : 'The job status is unavailable.'
        if (message.includes('expired')) {
          setError(message)
          setJob(undefined)
        } else {
          timer = setTimeout(poll, 2500)
        }
      }
    }
    timer = setTimeout(poll, 750)
    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [active, jobId])

  function resetSource() {
    setSource(undefined)
    setJob(undefined)
    setQuality('')
    setError('')
  }

  function changeMode(value: 'link' | 'file') {
    chosenMode.current = true
    if (mode !== value) resetSource()
    setMode(value)
  }

  async function inspect(event: FormEvent) {
    event.preventDefault()
    resetSource()
    setBusy(true)
    try {
      let result: Inspection
      if (mode === 'file') {
        if (!file) throw new Error('Choose a media file first.')
        if (file.size > (config?.max_upload_mib ?? 200) * 1024 ** 2) {
          throw new Error('Choose a smaller file.')
        }
        result = await request<Inspection>('/inspect/file', {
          method: 'POST',
          headers: { 'X-Media-Name': encodeURIComponent(file.name) },
          body: file,
        })
      } else {
        result = await post<Inspection>('/inspect/link', { url: link, authorized })
      }
      const type = result.video.length ? 'mp4' : 'mp3'
      setFormat(type)
      setQuality((type === 'mp4' ? result.video : result.audio)[0].id)
      setSource(result)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Inspection failed.')
    } finally {
      setBusy(false)
    }
  }

  async function start() {
    if (!source || !quality) return
    setError('')
    setJob(undefined)
    setBusy(true)
    try {
      setJob(await post<Job>('/jobs', { source_id: source.id, format, quality_id: quality }))
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'The job could not start.')
    } finally {
      setBusy(false)
    }
  }

  async function cancel() {
    if (!jobId) return
    setBusy(true)
    try {
      setJob(await request<Job>('/jobs/' + jobId, { method: 'DELETE' }))
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'The job could not be cancelled.')
    } finally {
      setBusy(false)
    }
  }

  function changeFormat(value: Format) {
    if (value === format) return
    setFormat(value)
    setQuality((value === 'mp4' ? source?.video : source?.audio)?.[0]?.id ?? '')
    setJob(undefined)
    setError('')
  }

  return (
    <div className="shell">
      <header className="masthead">
        <a href="/" className="brand" aria-label="Yosuu home">
          <svg aria-hidden="true" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M3 9v6m4-9v12m5-16v20m5-16v12m4-9v6" />
          </svg>
          Yosuu
        </a>
        <span className="masthead-label">Media workbench</span>
      </header>

      <main>
        <div className="intro">
          <h1>Media to MP4 or MP3</h1>
          <p>Inspect a link or file, choose the output quality, and save the result.</p>
        </div>

        <div className="workbench">
          <section className="source-panel" aria-labelledby="source-heading">
            <div className="panel-heading"><span>01</span><h2 id="source-heading">Source</h2></div>
            <form onSubmit={inspect}>
              <fieldset className="mode-switch" disabled={busy || active}>
                <legend className="sr-only">Source type</legend>
                <label><input type="radio" name="mode" checked={mode === 'link'} onChange={() => changeMode('link')} />Link</label>
                <label><input type="radio" name="mode" checked={mode === 'file'} onChange={() => changeMode('file')} />File</label>
              </fieldset>
              {mode === 'link' ? (
                <>
                  <label htmlFor="media-url">Video URL</label>
                  <input id="media-url" type="url" placeholder="https://" aria-describedby="link-availability" required value={link} maxLength={2048}
                    onChange={event => { setLink(event.target.value); resetSource() }} disabled={busy || active} />
                  <p className="hint" id="link-availability">
                    {config?.link_platforms.length
                      ? config.link_platforms.map(platform => names[platform]).join(' / ')
                      : 'Link processing is disabled on this instance. Choose File to continue.'}
                  </p>
                  <label className="consent"><input type="checkbox" required checked={authorized}
                    onChange={event => setAuthorized(event.target.checked)} disabled={busy || active} />
                    I have permission to download and convert this media.</label>
                </>
              ) : (
                <>
                  <label htmlFor="media-file">Media file</label>
                  <input id="media-file" type="file" accept="video/*,audio/*,.mkv" required
                    onChange={event => { setFile(event.target.files?.[0]); resetSource() }} disabled={busy || active} />
                  <p className="hint">Up to {config?.max_upload_mib ?? 200} MiB and 20 minutes per file.</p>
                </>
              )}
              <button className="raised" disabled={busy || active || (mode === 'link' && !config?.link_platforms.length)}>
                {busy && !source ? 'Inspecting…' : mode === 'file' ? 'Inspect file' : 'Inspect link'}
              </button>
            </form>

            <section className="readout" aria-labelledby="readout-heading">
              <h3 id="readout-heading">Source readout</h3>
              {source ? (
                <>
                  <p className="source-title" dir="auto">{source.title}</p>
                  <dl>
                    <div><dt>Source</dt><dd>{names[source.source]}</dd></div>
                    <div><dt>Duration</dt><dd>{source.duration === null ? 'Unknown' : durationLabel(source.duration)}</dd></div>
                  </dl>
                  {source.source === 'file' && file && <MediaPreview file={file} audio={!source.video.length} />}
                </>
              ) : <p>Choose a source to see its duration and available formats.</p>}
            </section>
          </section>

          <section className="export-panel" aria-labelledby="export-heading">
            <div className="panel-heading"><span>02</span><h2 id="export-heading">Export</h2></div>
            <fieldset className="format-switch" disabled={!source || busy || active}>
              <legend>Output format</legend>
              <label><input type="radio" name="format" checked={format === 'mp4'} disabled={source?.video.length === 0}
                onChange={() => changeFormat('mp4')} /><span>MP4<small>Video</small></span></label>
              <label><input type="radio" name="format" checked={format === 'mp3'} disabled={source?.audio.length === 0}
                onChange={() => changeFormat('mp3')} /><span>MP3<small>Audio</small></span></label>
            </fieldset>

            <label htmlFor="quality">{format === 'mp4' ? 'Resolution' : 'Target bitrate'}</label>
            <select id="quality" value={quality} disabled={!options?.length || busy || active}
              onChange={event => { setQuality(event.target.value); setJob(undefined); setError('') }}>
              {!options?.length && <option value="">Waiting for a source</option>}
              {options?.map(option => <option key={option.id} value={option.id}>{option.label} · {option.detail}</option>)}
            </select>
            {format === 'mp3' && <p className="hint">A higher bitrate cannot restore detail missing from the source.</p>}
            {source && !source.audio.length && <p className="hint">This source has no audio track.</p>}

            <div className="job-state" role="status" aria-live="polite">
              <span className={'lamp' + (active ? ' active' : '')} aria-hidden="true" />
              {job?.stage ?? (source ? 'Source ready' : 'Awaiting input')}
            </div>
            {(error || job?.error) && <p className="error" role="alert">{error || job?.error}</p>}
            <div className="actions">
              {job?.download_url
                ? <a className="primary" href={job.download_url}>Download {format.toUpperCase()}</a>
                : <button className="primary" type="button" disabled={!source || !quality || busy || active}
                  onClick={start}>Prepare {format.toUpperCase()}</button>}
              {active && <button className="secondary" type="button" disabled={busy} onClick={cancel}>Cancel job</button>}
            </div>
          </section>
        </div>
      </main>

      <footer>
        <span>Files expire after 15 minutes.</span>
        <nav aria-label="Policies">
          <a href="/terms.html">Terms</a>
          <a href="/privacy.html">Privacy</a>
          <a href="https://github.com/MeteAdam/Yosuu">GitHub</a>
        </nav>
      </footer>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<App />)
