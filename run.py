from pathlib import Path
import os
from dotenv import load_dotenv
import uvicorn

if __name__ == '__main__':
    load_dotenv(Path(__file__).parent / '.env')
    uvicorn.run('app.main:app', host=os.getenv('HOST', '127.0.0.1'),
                port=int(os.getenv('PORT', '8000')), workers=1)
