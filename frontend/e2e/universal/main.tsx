/** Isolated browser fixture; never imported by production entry points. */
import { createRoot } from 'react-dom/client';
import { UniversalWorkspace } from '../../src/components/UniversalWorkspace';
import '../../src/styles.css';

createRoot(document.getElementById('root')!).render(
  <main className="workspace-main tenant-operations" style={{ maxWidth: 1100, margin: '0 auto', padding: 24 }}>
    <h1>Подготовка КП — тестовый сценарий</h1>
    <p>Изолированный интерфейс с синтетическими ответами. Это не подключение к рабочей БД.</p>
    <UniversalWorkspace organizationId="00000000-0000-0000-0000-000000000001"
      organizationName="Тестовый поставщик" csrf="browser-mock-csrf" role="owner"/>
  </main>,
);
