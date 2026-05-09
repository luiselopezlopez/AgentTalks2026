import { useEffect, useRef, useState } from 'react'

type VoiceStatus = 'idle' | 'connecting' | 'connected' | 'ready' | 'listening' | 'processing' | 'speaking' | 'error'
interface ChatMsg { id: number; role: 'user' | 'assistant'; text: string }

const TARGET_SAMPLE_RATE = 24000

const STATUS_LABEL: Record<VoiceStatus, string> = {
  idle:        'Initializing avatar…',
  connecting:  'Connecting to voice AI…',
  connected:   'Voice AI connected',
  ready:       'Ready — speak now',
  listening:   'Listening…',
  processing:  'Thinking…',
  speaking:    'Speaking…',
  error:       'Connection error — reload to retry',
}

function App() {
  // Voice chat state
  const voiceOpen = true
  const [voiceStatus, setVoiceStatus] = useState<VoiceStatus>('idle')
  const [avatarReady, setAvatarReady] = useState(false)
  const [mediaActivationRequired, setMediaActivationRequired] = useState(false)
  const [messages, setMessages] = useState<ChatMsg[]>([])
  const wsRef = useRef<WebSocket | null>(null)
  const pcRef = useRef<RTCPeerConnection | null>(null)
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const micStreamRef = useRef<MediaStream | null>(null)
  const audioContextRef = useRef<AudioContext | null>(null)
  const audioSourceRef = useRef<MediaStreamAudioSourceNode | null>(null)
  const audioProcessorRef = useRef<ScriptProcessorNode | null>(null)

  const encodePcm16 = (input: Float32Array) => {
    const pcm = new Int16Array(input.length)
    for (let index = 0; index < input.length; index += 1) {
      const sample = Math.max(-1, Math.min(1, input[index] ?? 0))
      pcm[index] = sample < 0 ? sample * 0x8000 : sample * 0x7fff
    }

    const bytes = new Uint8Array(pcm.buffer)
    let binary = ''
    const chunkSize = 0x8000
    for (let offset = 0; offset < bytes.length; offset += chunkSize) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize))
    }
    return btoa(binary)
  }

  const downsampleTo24k = (input: Float32Array, sampleRate: number) => {
    if (sampleRate === TARGET_SAMPLE_RATE) return input

    const ratio = sampleRate / TARGET_SAMPLE_RATE
    const outputLength = Math.max(1, Math.round(input.length / ratio))
    const output = new Float32Array(outputLength)

    let inputOffset = 0
    for (let outputIndex = 0; outputIndex < outputLength; outputIndex += 1) {
      const nextOffset = Math.min(input.length, Math.round((outputIndex + 1) * ratio))
      let accumulator = 0
      let count = 0
      for (let index = inputOffset; index < nextOffset; index += 1) {
        accumulator += input[index] ?? 0
        count += 1
      }
      output[outputIndex] = count > 0 ? accumulator / count : input[inputOffset] ?? 0
      inputOffset = nextOffset
    }

    return output
  }

  const stopMicrophoneCapture = () => {
    audioProcessorRef.current?.disconnect()
    audioSourceRef.current?.disconnect()
    micStreamRef.current?.getTracks().forEach((track) => track.stop())
    void audioContextRef.current?.close()

    audioProcessorRef.current = null
    audioSourceRef.current = null
    micStreamRef.current = null
    audioContextRef.current = null
  }

  const resumeMediaPlayback = async () => {
    const tasks: Array<Promise<unknown>> = []

    if (audioContextRef.current && audioContextRef.current.state === 'suspended') {
      tasks.push(audioContextRef.current.resume())
    }

    if (videoRef.current && videoRef.current.srcObject) {
      tasks.push(videoRef.current.play())
    }

    if (audioRef.current && audioRef.current.srcObject) {
      tasks.push(audioRef.current.play())
    }

    if (tasks.length === 0) {
      setMediaActivationRequired(false)
      return
    }

    const results = await Promise.allSettled(tasks)
    const hasFailures = results.some((result) => result.status === 'rejected')
    const audioStillSuspended = audioContextRef.current?.state === 'suspended'
    setMediaActivationRequired(hasFailures || Boolean(audioStillSuspended))
  }

  const startMicrophoneCapture = async (ws: WebSocket) => {
    if (micStreamRef.current || audioContextRef.current) return

    const mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false,
    })

    const audioContext = new AudioContext()
    const source = audioContext.createMediaStreamSource(mediaStream)
    const processor = audioContext.createScriptProcessor(4096, 1, 1)

    processor.onaudioprocess = (event) => {
      if (ws.readyState !== WebSocket.OPEN) return

      const mono = event.inputBuffer.getChannelData(0)
      const downsampled = downsampleTo24k(mono, audioContext.sampleRate)
      ws.send(JSON.stringify({ type: 'input_audio', audio: encodePcm16(downsampled) }))
    }

    source.connect(processor)
    processor.connect(audioContext.destination)

    micStreamRef.current = mediaStream
    audioContextRef.current = audioContext
    audioSourceRef.current = source
    audioProcessorRef.current = processor

    await resumeMediaPlayback()
  }

  const closePeerConnection = () => {
    if (pcRef.current) {
      pcRef.current.getSenders().forEach((sender) => sender.track?.stop())
      pcRef.current.getReceivers().forEach((receiver) => receiver.track?.stop())
      pcRef.current.close()
      pcRef.current = null
    }
  }

  const waitForIceGathering = async (pc: RTCPeerConnection) => {
    await new Promise<void>((resolve) => {
      if (pc.iceGatheringState === 'complete') {
        resolve()
        return
      }

      const onIceState = () => {
        if (pc.iceGatheringState === 'complete') {
          pc.removeEventListener('icegatheringstatechange', onIceState)
          resolve()
        }
      }

      pc.addEventListener('icegatheringstatechange', onIceState)
      setTimeout(() => {
        pc.removeEventListener('icegatheringstatechange', onIceState)
        resolve()
      }, 5000)
    })
  }

  const handleAvatarIceServers = async (servers: Array<{ urls: string[]; username?: string; credential?: string }>) => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return

    closePeerConnection()
    setAvatarReady(false)

    const pc = new RTCPeerConnection({
      iceServers: servers.map((server) => ({
        urls: server.urls,
        username: server.username,
        credential: server.credential,
      })),
      bundlePolicy: 'max-bundle',
    })

    pc.ontrack = (event) => {
      if (event.track.kind === 'video' && videoRef.current) {
        videoRef.current.srcObject = event.streams[0]
        void resumeMediaPlayback()
      }
      if (event.track.kind === 'audio' && audioRef.current) {
        audioRef.current.srcObject = event.streams[0]
        void resumeMediaPlayback()
      }
    }

    pc.oniceconnectionstatechange = () => {
      if (pc.iceConnectionState === 'connected' || pc.iceConnectionState === 'completed') {
        setAvatarReady(true)
        void resumeMediaPlayback()
      }
      if (pc.iceConnectionState === 'failed' || pc.iceConnectionState === 'disconnected') {
        setAvatarReady(false)
      }
    }

    pc.addTransceiver('video', { direction: 'recvonly' })
    pc.addTransceiver('audio', { direction: 'recvonly' })
    pcRef.current = pc

    const offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    await waitForIceGathering(pc)

    const localSdp = pc.localDescription?.sdp ?? ''
    const encodedOffer = btoa(JSON.stringify({ type: 'offer', sdp: localSdp }))
    ws.send(JSON.stringify({ type: 'avatar_offer', sdp: encodedOffer }))
  }

  // Auto-scroll transcript
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [messages])

  useEffect(() => {
    if (!mediaActivationRequired) return

    const unlockMedia = () => {
      void resumeMediaPlayback()
    }

    window.addEventListener('pointerdown', unlockMedia)
    window.addEventListener('keydown', unlockMedia)

    return () => {
      window.removeEventListener('pointerdown', unlockMedia)
      window.removeEventListener('keydown', unlockMedia)
    }
  }, [mediaActivationRequired])

  // WebSocket lifecycle — opens when panel opens, closes when panel closes
  useEffect(() => {
    if (!voiceOpen) return

    setVoiceStatus('connecting')
    setAvatarReady(false)
    setMessages([])

    const ws = new WebSocket('ws://127.0.0.1:8765/ws')
    wsRef.current = ws

    ws.onmessage = (ev) => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const msg = JSON.parse(ev.data as string) as any

      if (msg.type === 'status') {
        setVoiceStatus(msg.value as VoiceStatus)
      } else if (msg.type === 'connected') {
        setVoiceStatus('connected')
        void startMicrophoneCapture(ws).catch(() => {
          setVoiceStatus('error')
        })
      } else if (msg.type === 'avatar_ice_servers') {
        void handleAvatarIceServers(msg.servers as Array<{ urls: string[]; username?: string; credential?: string }>)
      } else if (msg.type === 'avatar_connecting') {
        setAvatarReady(false)
      } else if (msg.type === 'avatar_answer') {
        const payload = JSON.parse(atob(msg.sdp as string)) as RTCSessionDescriptionInit
        if (pcRef.current) {
          void pcRef.current.setRemoteDescription(new RTCSessionDescription(payload))
        }
      } else if (msg.type === 'avatar_ready') {
        setAvatarReady(true)
      } else if (msg.type === 'avatar_error') {
        setAvatarReady(false)
      } else if (msg.type === 'transcript') {
        setMessages((prev) => [
          ...prev,
          { id: Date.now() + Math.random(), role: msg.role as 'user' | 'assistant', text: msg.text },
        ])
      } else if (msg.type === 'error') {
        setVoiceStatus('error')
      }
    }

    ws.onclose = () => {
      setVoiceStatus('idle')
      setAvatarReady(false)
      setMediaActivationRequired(false)
      stopMicrophoneCapture()
      closePeerConnection()
      wsRef.current = null
    }

    ws.onerror = () => {
      setVoiceStatus('error')
      setAvatarReady(false)
      setMediaActivationRequired(false)
      stopMicrophoneCapture()
    }

    return () => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'stop' }))
      }
      stopMicrophoneCapture()
      ws.close()
      closePeerConnection()
      wsRef.current = null
    }
  }, [voiceOpen])

  const voicePanelClassName = 'voice-drawer voice-drawer--fullscreen'

  return (
    <div className="page-shell page-shell--avatar-mode">
      <div className="backdrop backdrop-one" />
      <div className="backdrop backdrop-two" />

      <main className="app-frame app-frame--avatar-mode">
        <section className="avatar-page-shell">
          <div className="avatar-page-copy">
            <p className="eyebrow">Voice companion</p>
            <h1>Talk to your AI companion</h1>
            <p className="hero-text">
              The session starts automatically. Talk naturally about everyday topics, technology, ideas, projects, or documentation.
            </p>
          </div>
        </section>
      </main>

      {/* ── Voice chat drawer ── */}
      {voiceOpen && (
        <div className={voicePanelClassName} role="dialog" aria-label="Voice companion">
          <div className="voice-drawer-head">
            <span className={`voice-orb voice-orb--${voiceStatus}`} aria-hidden="true" />
            <div className="voice-drawer-headings">
              <p className="voice-drawer-title">AI Companion</p>
              <p className="voice-drawer-status">{STATUS_LABEL[voiceStatus]}</p>
            </div>
          </div>

          <div className="avatar-stage" aria-live="polite">
            <video ref={videoRef} className="avatar-video" autoPlay playsInline />
            <audio ref={audioRef} autoPlay />
            {!avatarReady && (
              <p className="avatar-loading">Connecting avatar stream…</p>
            )}
            {mediaActivationRequired && (
              <button className="avatar-activation" type="button" onClick={() => void resumeMediaPlayback()}>
                Tap to enable avatar audio and video
              </button>
            )}
          </div>

          <div className="voice-transcript" ref={scrollRef}>
            {messages.length === 0 && (
              <p className="voice-hint">
                {voiceStatus === 'connecting' || voiceStatus === 'connected'
                  ? 'Starting your AI companion…'
                  : voiceStatus === 'error'
                  ? undefined
                  : 'Say hello, ask a question, or explore an idea.'}
              </p>
            )}

            {voiceStatus === 'error' && (
              <p className="voice-hint voice-hint--error">
                Could not connect to the voice server. Make sure it is running:
                <code>cd voice &amp;&amp; python server.py</code>
              </p>
            )}

            {messages.map((msg) => (
              <div key={msg.id} className={`voice-bubble-item voice-bubble-item--${msg.role}`}>
                {msg.text}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

export default App