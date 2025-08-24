export default async (req, context) => {
  const AURORE_BLOBS_TOKEN = process.env.AURORE_BLOBS_TOKEN;
  const blobUrl = `https://api.netlify.com/api/v1/blobs/${context.params.path}`;

  const response = await fetch(blobUrl, {
    method: req.method,
    headers: {
      'Content-Type': req.headers.get('content-type') || 'application/json',
      'Authorization': `Bearer ${AURORE_BLOBS_TOKEN}`
    },
    body: req.method !== 'GET' ? await req.text() : undefined
  });

  return new Response(await response.text(), {
    status: response.status,
    headers: { 'Content-Type': 'application/json' }
  });
};
