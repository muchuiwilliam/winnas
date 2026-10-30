## Run the server

```bash
uvicorn main:app
```

## Make a request
```bash
curl localhost:8000/city/Nairobi | jq . > result.json
```
