export type Platform = 'youtube' | 'instagram' | 'tiktok'
export type Format = 'mp4' | 'mp3'

export interface Quality {
  id: string
  label: string
  detail: string
}

export interface Inspection {
  id: string
  title: string
  source: Platform | 'file'
  duration: number | null
  video: Quality[]
  audio: Quality[]
}

export interface Job {
  id: string
  status: 'queued' | 'running' | 'ready' | 'failed' | 'cancelled'
  stage: string
  error: string | null
  download_url: string | null
}

export interface Config {
  link_platforms: Platform[]
  max_upload_mib: number
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch('/api' + path, init)
  const data = await response.json().catch(() => null)
  if (!response.ok || data === null) {
    throw new Error(typeof data?.detail === 'string' ? data.detail : 'The workbench could not complete the request.')
  }
  return data
}

export function post<T>(path: string, body: object): Promise<T> {
  return request(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}
