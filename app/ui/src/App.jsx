import { useState, useEffect } from 'react'
import Tab1Config from './components/Tab1Config'
import Tab2Credentials from './components/Tab2Credentials'
import ChannelList from './components/ChannelList'
import { api } from './api/agent1'

const TABS = ['Channel Config', 'Credentials']

export default function App() {
  const [view,         setView]         = useState('list')
  const [activeTab,    setActiveTab]    = useState(0)
  const [channelId,    setChannelId]    = useState(null)
  const [languages,    setLanguages]    = useState([])
  const [platforms,    setPlatforms]    = useState([])
  const [userLanguage, setUserLanguage] = useState('en')

  useEffect(() => {
    api.getMe()
      .then(u => setUserLanguage(u.primary_language ?? 'en'))
      .catch(console.error)
  }, [])

  const openCreate = () => {
    setChannelId(null)
    setLanguages([])
    setPlatforms([])
    setActiveTab(0)
    setView('setup')
  }

  const openEdit = (id) => {
    setChannelId(id)
    setActiveTab(0)
    setView('setup')
  }

  const backToList = () => {
    setView('list')
    setActiveTab(0)
  }

  if (view === 'list') {
    return (
      <div className="app">
        <header className="app-header">
          <h1>Content Factory</h1>
        </header>
        <ChannelList onEdit={openEdit} onCreate={openCreate} />
      </div>
    )
  }

  return (
    <div className="app">
      <header className="app-header">
        <button type="button" className="btn-secondary btn-sm" onClick={backToList}>← Channels</button>
        <h1>Content Factory — Channel Setup</h1>
      </header>

      <nav className="tab-nav">
        {TABS.map((label, i) => (
          <span key={i} className={`tab-label${activeTab === i ? ' active' : ''}`}>
            {label}
          </span>
        ))}
      </nav>

      <main>
        {activeTab === 0 && (
          <Tab1Config
            channelId={channelId}
            userLanguage={userLanguage}
            onChannelCreated={setChannelId}
            onLanguagesChange={setLanguages}
            onPlatformsChange={setPlatforms}
            onNext={() => setActiveTab(1)}
            onCancel={backToList}
          />
        )}
        {activeTab === 1 && (
          <Tab2Credentials
            channelId={channelId}
            languages={languages}
            platforms={platforms}
            onBack={() => setActiveTab(0)}
            onCancel={backToList}
          />
        )}
      </main>
    </div>
  )
}
