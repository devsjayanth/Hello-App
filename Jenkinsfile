pipeline {
    agent { label 'jenkins-agent' }

    environment {
        // ────────────────────────────────────────────────
        GIT_URL         = 'https://github.com/devsjayanth/Hello-App.git'
        GIT_CRED        = 'github-cred'       
        SONAR_CRED      = 'sonar-cred'        
        HARBOR_URL      = '10.0.2.150:80'      
        HARBOR_CRED     = 'harbor-cred'       
        APP_NAME        = 'hello-app'         
        APP_PORT        = '9001'              
        APP_INTERNAL    = '8000'              
        DEV_SERVER      = 'dev-server'        
        GITOPS_REPO     = 'https://github.com/devsjayanth/Hello-App-GitOps.git'
        GITOPS_BRANCH   = 'main'
        GITOPS_CRED     = 'github-cred'       
        MANIFEST_PATH   = 'hello-app-k8s/deployment.yml'
        IMAGE_TAG       = "${BUILD_NUMBER}"
        IMAGE_LATEST    = "latest"
        
        // UPDATED: Uses the default 'library' project in Harbor
        IMAGE_NAME      = "${HARBOR_URL}/library/${APP_NAME}"
        // ────────────────────────────────────────────────
    }

    stages {
        stage('Clean Workspace') {
            steps {
                cleanWs()
            }
        }

        stage('Checkout') {
            steps {
                checkout([
                    $class: 'GitSCM',
                    branches: [[name: '*/main']],
                    userRemoteConfigs: [[
                        url: GIT_URL,
                        credentialsId: GIT_CRED
                    ]]
                ])
            }
        }

        stage('SonarQube Analysis') {
            steps {
                withSonarQubeEnv(installationName: 'sonar-scanner', credentialsId: SONAR_CRED) {
                    sh 'sonar-scanner -Dsonar.projectKey=${APP_NAME} -Dsonar.sources=. -Dsonar.qualitygate.wait=true'
                }
            }
        }

        stage('Docker Build') {
            steps {
                script {
                    docker.build("${IMAGE_NAME}:${IMAGE_TAG}", "--no-cache --pull .")
                    sh "docker tag ${IMAGE_NAME}:${IMAGE_TAG} ${IMAGE_NAME}:${IMAGE_LATEST}"
                }
            }
        }

        stage('Trivy Scan') {
            steps {
                sh "trivy image --exit-code 1 --severity HIGH,CRITICAL --ignore-unfixed ${IMAGE_NAME}:${IMAGE_TAG}"
            }
        }

        stage('Push to Harbor') {
            steps {
                withCredentials([
                    usernamePassword(
                        credentialsId: HARBOR_CRED,
                        usernameVariable: 'HARBOR_USER',
                        passwordVariable: 'HARBOR_PASS'
                    )
                ]) {
                    sh "echo \$HARBOR_PASS | docker login ${HARBOR_URL} -u \$HARBOR_USER --password-stdin"
                    sh "docker push ${IMAGE_NAME}:${IMAGE_TAG}"
                    sh "docker push ${IMAGE_NAME}:${IMAGE_LATEST}"
                }
            }
        }

        stage('Staging Deploy to Dev-Server') {
            steps {
                sh """ssh ${DEV_SERVER} '
                    docker pull ${IMAGE_NAME}:latest
                    docker stop ${APP_NAME} 2>/dev/null || true
                    docker rm ${APP_NAME} 2>/dev/null || true
                    docker run -d --name ${APP_NAME} \
                        --restart unless-stopped \
                        -p ${APP_PORT}:${APP_INTERNAL} \
                        ${IMAGE_NAME}:latest
                    docker image prune -f
                '"""
            }
        }

        stage('Health Check') {
            steps {
                sh """ssh ${DEV_SERVER} '
                    for i in \$(seq 1 12); do
                        curl -sf http://127.0.0.1:${APP_PORT}/ && exit 0
                        sleep 5
                    done
                    exit 1
                '"""
            }
        }

        stage('Approve for Production') {
            steps {
                input message: 'Deploy to Prod via ArgoCD?', ok: 'Yes, deploy to production'
            }
        }

        stage('Push to GitOps Repo') {
            steps {
                withCredentials([usernamePassword(
                    credentialsId: GITOPS_CRED,
                    usernameVariable: 'GIT_USER',
                    passwordVariable: 'GIT_PASS'
                )]) {
                    script {
                        def encodedPass = java.net.URLEncoder.encode(GIT_PASS, "UTF-8")
                        def repoUrl = GITOPS_REPO.replaceFirst(/^https?:\/\//, "https://${GIT_USER}:${encodedPass}@")
                        sh """
                            rm -rf gitops
                            git clone ${repoUrl} gitops
                            cd gitops
                            sed -i 's|image:.*|image: ${IMAGE_NAME}:${IMAGE_TAG}|' ${MANIFEST_PATH}
                            git config user.name 'Jenkins'
                            git config user.email 'jenkins@devops'
                            git add ${MANIFEST_PATH}
                            git commit -m 'deploy: bump ${APP_NAME} to ${IMAGE_TAG}'
                            git push
                        """
                    }
                }
            }
        }
    }
}