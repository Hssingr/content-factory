import { useState, useEffect } from 'react'
import { api } from '../api/agent1'

export default function ChannelList({ onEdit, onCreate }) {
  const [channels, setChannels] = useState([])
  const [loading,  setLoading]  = useState(true)
  const [error,    setError]    = useState('')

  useEffect(() => {
    api.listChannels()
      .then(setChannels)
      .catch(e => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

  const handleDelete = async (ch) => {
    if (!window.confirm(`Delete channel "${ch.name}"? This cannot be undone.`)) return
    try {
      await api.deleteChannel(ch.id)
      setChannels(prev => prev.filter(c => c.id !== ch.id))
    } catch (e) {
      setError(e.message)
    }
  }

  return (
    <div className="channel-list-view">
      <div className="channel-list-header">
        <h2>My Channels</h2>
        <button type="button" className="btn-primary" onClick={onCreate}>+ Create Channel</button>
      </div>

      {error && <div className="error-banner">{error}</div>}

      {loading && <p className="placeholder">Loading channels…</p>}

      {!loading && channels.length === 0 && (
        <div className="channel-list-empty">
          <p>No channels yet.</p>
          <button type="button" className="btn-primary" onClick={onCreate}>Create your first channel</button>
        </div>
      )}

      {!loading && channels.length > 0 && (
        <table className="channel-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Niche</th>
              <th>Status</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {channels.map(ch => (
              <tr key={ch.id}>
                <td className="channel-name-cell">{ch.name || <span className="channel-draft-name">Untitled</span>}</td>
                <td className="channel-niche-cell">{ch.niche}</td>
                <td>
                  <span className={`status-badge ${ch.active ? 'status-active' : 'status-draft'}`}>
                    {ch.active ? 'ACTIVE' : 'DRAFT'}
                  </span>
                </td>
                <td className="channel-actions-cell">
                  <button
                    type="button"
                    className="btn-secondary btn-sm"
                    onClick={() => onEdit(ch.id)}
                  >
                    {ch.active ? 'View' : 'Edit'}
                  </button>
                  <button
                    type="button"
                    className="btn-secondary btn-sm btn-danger"
                    onClick={() => handleDelete(ch)}
                    disabled={ch.active}
                    title={ch.active ? 'Cannot delete an active channel' : 'Delete channel'}
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
